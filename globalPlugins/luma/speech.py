"""Voice input for Luma: microphone recording + provider transcription.

Records audio from the default microphone via the Windows Multimedia
API (winmm.dll, zero external dependencies), detects silence to stop,
and transcribes the recording through the active provider's transcription
endpoint.
"""

from __future__ import annotations

import ctypes
import io
import logging
import struct
import threading
import time
import wave
from ctypes import wintypes

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 / winmm constants and structures
# ---------------------------------------------------------------------------

_WAVE_FORMAT_PCM = 1
_WAVE_MAPPER = 0xFFFFFFFF  # UINT(-1) — default recording device
_CALLBACK_NULL = 0x00000000
_WHDR_DONE = 0x00000001

_SAMPLE_RATE = 16_000  # 16 kHz — Whisper's preferred rate
_CHANNELS = 1  # mono
_BITS_PER_SAMPLE = 16
_BLOCK_ALIGN = _CHANNELS * _BITS_PER_SAMPLE // 8  # 2
_BYTES_PER_SEC = _SAMPLE_RATE * _BLOCK_ALIGN  # 32 000

# Each buffer holds 0.25 s of audio.
_BUF_DURATION_S = 0.25
_BUF_SIZE = int(_BYTES_PER_SEC * _BUF_DURATION_S)  # 8 000 bytes
_NUM_BUFFERS = 4


class _WAVEFORMATEX(ctypes.Structure):
	_fields_ = [
		("wFormatTag", ctypes.c_ushort),
		("nChannels", ctypes.c_ushort),
		("nSamplesPerSec", ctypes.c_ulong),
		("nAvgBytesPerSec", ctypes.c_ulong),
		("nBlockAlign", ctypes.c_ushort),
		("wBitsPerSample", ctypes.c_ushort),
		("cbSize", ctypes.c_ushort),
	]


class _WAVEHDR(ctypes.Structure):
	_fields_ = [
		("lpData", ctypes.c_void_p),
		("dwBufferLength", ctypes.c_ulong),
		("dwBytesRecorded", ctypes.c_ulong),
		("dwUser", ctypes.POINTER(ctypes.c_ulong)),
		("dwFlags", ctypes.c_ulong),
		("dwLoops", ctypes.c_ulong),
		("lpNext", ctypes.c_void_p),
		("reserved", ctypes.POINTER(ctypes.c_ulong)),
	]


_winmm = ctypes.windll.winmm


# ---------------------------------------------------------------------------
# Microphone helpers
# ---------------------------------------------------------------------------

def is_microphone_available() -> bool:
	"""Check whether a recording device is present.

	Safe to call from any thread, including the main thread.
	"""
	return _winmm.waveInGetNumDevs() > 0


def _compute_rms(data: bytes) -> int:
	"""Compute RMS amplitude of 16-bit signed PCM data."""
	count = len(data) // 2
	if count == 0:
		return 0
	total = sum(s * s for (s,) in struct.iter_unpack("<h", data))
	return int((total / count) ** 0.5)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _record_until_silence(
	*,
	silence_timeout: float = 2.0,
	max_duration: float = 60.0,
	noise_multiplier: float = 3.0,
	calibration_s: float = 0.75,
	stop_event: threading.Event | None = None,
) -> bytes:
	"""Record from the default microphone until silence or stop signal.

	The first *calibration_s* seconds are used to measure the ambient
	noise floor.  The speech threshold is then set to
	``max(noise_floor * noise_multiplier, 200)``.

	Stops when any of these conditions is met (in priority order):
	1. *stop_event* is set (user pressed Stop).
	2. Speech was detected and then *silence_timeout* seconds of silence
	   followed.
	3. *max_duration* seconds elapsed.

	Returns raw 16-bit mono 16 kHz PCM bytes (may be empty if stopped
	immediately).
	Raises :class:`RuntimeError` on device errors.

	Must be called from a background thread.
	"""
	hwi = ctypes.c_void_p()
	fmt = _WAVEFORMATEX(
		wFormatTag=_WAVE_FORMAT_PCM,
		nChannels=_CHANNELS,
		nSamplesPerSec=_SAMPLE_RATE,
		nAvgBytesPerSec=_BYTES_PER_SEC,
		nBlockAlign=_BLOCK_ALIGN,
		wBitsPerSample=_BITS_PER_SAMPLE,
		cbSize=0,
	)

	rc = _winmm.waveInOpen(
		ctypes.byref(hwi), _WAVE_MAPPER, ctypes.byref(fmt),
		0, 0, _CALLBACK_NULL,
	)
	if rc != 0:
		# Translators: Spoken when the microphone cannot be opened.
		raise RuntimeError(_("Could not open the microphone (error {code}).").format(code=rc))

	# Allocate buffers and keep references alive.
	raw_bufs: list[ctypes.Array] = []
	headers: list[_WAVEHDR] = []
	for _ in range(_NUM_BUFFERS):
		buf = ctypes.create_string_buffer(_BUF_SIZE)
		raw_bufs.append(buf)
		hdr = _WAVEHDR(
			lpData=ctypes.addressof(buf),
			dwBufferLength=_BUF_SIZE,
		)
		_winmm.waveInPrepareHeader(hwi, ctypes.byref(hdr), ctypes.sizeof(hdr))
		_winmm.waveInAddBuffer(hwi, ctypes.byref(hdr), ctypes.sizeof(hdr))
		headers.append(hdr)

	_winmm.waveInStart(hwi)

	pcm_chunks: list[bytes] = []
	speech_detected = False
	silence_elapsed = 0.0
	start = time.monotonic()

	# -- Noise floor calibration --
	calibration_rms: list[int] = []
	rms_threshold = 200  # absolute minimum, overridden after calibration
	calibrating = True
	_dbg_chunks = 0  # debug counter

	try:
		while True:
			# Priority 1: manual stop.
			if stop_event and stop_event.is_set():
				break

			elapsed = time.monotonic() - start

			# Priority 3: max duration.
			if elapsed >= max_duration:
				break

			# End calibration phase after calibration_s seconds.
			if calibrating and elapsed >= calibration_s:
				calibrating = False
				if calibration_rms:
					noise_floor = sum(calibration_rms) / len(calibration_rms)
					rms_threshold = max(int(noise_floor * noise_multiplier), 200)
					# Cap the threshold so that residual speaker output or
					# environmental noise during calibration cannot push it
					# above a level that normal speech would fail to exceed.
					rms_threshold = min(rms_threshold, 1500)
				log.info(
					"Noise calibration: samples=%d, noise_floor=%.0f, threshold=%d",
					len(calibration_rms),
					(sum(calibration_rms) / len(calibration_rms)) if calibration_rms else 0,
					rms_threshold,
				)

			for hdr in headers:
				if not (hdr.dwFlags & _WHDR_DONE):
					continue

				recorded = hdr.dwBytesRecorded
				if recorded > 0:
					chunk = ctypes.string_at(hdr.lpData, recorded)
					pcm_chunks.append(chunk)
					rms = _compute_rms(chunk)
					_dbg_chunks += 1

					if calibrating:
						calibration_rms.append(rms)
					else:
						if rms >= rms_threshold:
							speech_detected = True
							silence_elapsed = 0.0
						else:
							silence_elapsed += _BUF_DURATION_S

					# Log every ~1 s (every 4 chunks at 0.25 s each).
					if _dbg_chunks % 4 == 0:
						log.info(
							"chunk=%d rms=%d thr=%d speech=%s silence=%.2fs",
							_dbg_chunks, rms, rms_threshold,
							speech_detected, silence_elapsed,
						)

				# Re-queue the buffer.  Only clear WHDR_DONE (0x1);
				# WHDR_PREPARED (0x2) must stay set or waveInAddBuffer fails.
				hdr.dwFlags &= ~_WHDR_DONE
				hdr.dwBytesRecorded = 0
				rc = _winmm.waveInAddBuffer(
					hwi, ctypes.byref(hdr), ctypes.sizeof(hdr),
				)
				if rc != 0:
					log.warning("waveInAddBuffer failed: rc=%d", rc)

			# Priority 2: silence after speech.
			if speech_detected and silence_elapsed >= silence_timeout:
				log.info("Silence detected after speech, stopping.")
				break

			time.sleep(0.02)
	finally:
		_winmm.waveInStop(hwi)
		# Small delay so the driver releases buffers.
		time.sleep(0.05)
		for hdr in headers:
			_winmm.waveInUnprepareHeader(hwi, ctypes.byref(hdr), ctypes.sizeof(hdr))
		_winmm.waveInClose(hwi)

	return b"".join(pcm_chunks)


# ---------------------------------------------------------------------------
# WAV encoding (in-memory)
# ---------------------------------------------------------------------------

def _pcm_to_wav(pcm_data: bytes) -> bytes:
	"""Wrap raw 16-bit mono 16 kHz PCM in a WAV container."""
	buf = io.BytesIO()
	with wave.open(buf, "wb") as wf:
		wf.setnchannels(_CHANNELS)
		wf.setsampwidth(_BITS_PER_SAMPLE // 8)
		wf.setframerate(_SAMPLE_RATE)
		wf.writeframes(pcm_data)
	return buf.getvalue()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_speech_language() -> str:
	"""Return the language code to use for speech transcription.

	Checks the ``speech_language`` setting first.  If empty, falls
	back to NVDA's configured language (ISO-639-1 prefix).
	"""
	from . import config as luma_config

	lang = luma_config.get_setting("speech_language", "")
	if lang:
		return lang

	# Fall back to NVDA's language.
	try:
		import languageHandler
		nvda_lang = languageHandler.getLanguage()
		if nvda_lang:
			# NVDA returns e.g. "en" or "de_DE" — take the primary subtag.
			return nvda_lang.split("_")[0]
	except Exception:
		pass
	return ""


def record_and_transcribe(
	provider,
	*,
	model: str = "",
	language: str = "",
	silence_timeout: float = 2.0,
	timeout: int = 30,
	stop_event: threading.Event | None = None,
) -> str:
	"""Record from the microphone and transcribe via the provider.

	*provider* must be a :class:`~providers.base.Provider` instance
	whose :attr:`supports_transcription` is ``True``.
	*model* is forwarded to the provider's transcription method.

	This function **blocks** and must be called from a background thread.

	If *stop_event* is set, recording stops immediately and whatever
	audio was captured is sent for transcription.

	Returns the transcribed text string.
	"""
	import speech as nvda_speech
	import tones
	import ui
	import wx

	# Let "Listening..." finish, then silence NVDA so remaining
	# speaker output is not captured during noise-floor calibration.
	time.sleep(0.5)
	wx.CallAfter(nvda_speech.cancelSpeech)
	time.sleep(0.1)

	# Signal recording start.
	wx.CallAfter(tones.beep, 880, 100)
	time.sleep(0.15)

	pcm_data = _record_until_silence(
		silence_timeout=silence_timeout,
		stop_event=stop_event,
	)

	# Minimum ~0.1 s of audio needed for useful transcription.
	min_bytes = int(_BYTES_PER_SEC * 0.1)
	if len(pcm_data) < min_bytes:
		# Translators: Spoken when recording was too short to transcribe.
		raise RuntimeError(_("No speech detected."))

	wav_data = _pcm_to_wav(pcm_data)

	log.debug(
		"Recorded %.1f s of audio (%d bytes WAV).",
		len(pcm_data) / _BYTES_PER_SEC,
		len(wav_data),
	)

	# Translators: Spoken while the recording is being transcribed.
	wx.CallAfter(ui.message, _("Transcribing..."))

	# Resolve language if not passed explicitly.
	if not language:
		language = get_speech_language()

	return provider.transcribe_audio(
		wav_data, model=model, language=language, timeout=timeout,
	)
