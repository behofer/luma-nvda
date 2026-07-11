"""Webcam camera capture for Luma.

Provides functions to enumerate webcams and capture a single frame
as a base64-encoded PNG image using ESCAPI 3.0 (Windows Media Foundation).

The ``capture_camera`` function is **blocking** (1-3 seconds) and must
only be called from a background thread via ``worker.run_in_background``.
"""

from __future__ import annotations

import base64
import ctypes
import io
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ESCAPI ctypes bindings
# ---------------------------------------------------------------------------

# Path to the bundled ESCAPI 64-bit DLL.
_DLL_PATH = os.path.join(os.path.dirname(__file__), "lib", "escapi.dll")

# Lazily loaded DLL handle.
_escapi = None

# Tracks which threads have had ESCAPI's initCOM() run.  COM/Media
# Foundation initialisation is per-thread; without it, device
# enumeration silently reports zero cameras on many systems (seen with
# e.g. Lenovo EasyCamera devices), even though the camera works in
# other applications.
_com_initialized = threading.local()


class _SimpleCapParams(ctypes.Structure):
	"""ESCAPI ``SimpleCapParams`` struct — matches the C definition."""

	_fields_ = [
		("buf", ctypes.POINTER(ctypes.c_int)),
		("width", ctypes.c_int),
		("height", ctypes.c_int),
		("fps", ctypes.c_float),
	]


def _load_escapi():
	"""Load the ESCAPI DLL and configure function signatures.

	Returns the loaded DLL or ``None`` on failure.  The result is cached
	so the DLL is only loaded once per session.
	"""
	global _escapi
	if _escapi is not None:
		return _escapi

	if not os.path.isfile(_DLL_PATH):
		log.error("ESCAPI DLL not found at %s", _DLL_PATH)
		return None

	try:
		dll = ctypes.cdll.LoadLibrary(_DLL_PATH)
	except OSError:
		log.exception("Failed to load ESCAPI DLL")
		return None

	# void initCOM()
	dll.initCOM.restype = None
	dll.initCOM.argtypes = []

	# int countCaptureDevices()
	dll.countCaptureDevices.restype = ctypes.c_int
	dll.countCaptureDevices.argtypes = []

	# void getCaptureDeviceName(unsigned int deviceno, char *namebuffer, int bufferlength)
	dll.getCaptureDeviceName.restype = None
	dll.getCaptureDeviceName.argtypes = [ctypes.c_uint, ctypes.c_char_p, ctypes.c_int]

	# int initCapture(unsigned int deviceno, struct SimpleCapParams *aParams)
	dll.initCapture.restype = ctypes.c_int
	dll.initCapture.argtypes = [ctypes.c_uint, ctypes.POINTER(_SimpleCapParams)]

	# void doCapture(unsigned int deviceno)
	dll.doCapture.restype = None
	dll.doCapture.argtypes = [ctypes.c_uint]

	# int isCaptureDone(unsigned int deviceno)
	dll.isCaptureDone.restype = ctypes.c_int
	dll.isCaptureDone.argtypes = [ctypes.c_uint]

	# void deinitCapture(unsigned int deviceno)
	dll.deinitCapture.restype = None
	dll.deinitCapture.argtypes = [ctypes.c_uint]

	_escapi = dll
	return _escapi


def _ensure_com(dll) -> None:
	"""Run ESCAPI's ``initCOM()`` once per calling thread.

	ESCAPI 3.0 requires COM / Media Foundation to be initialised on the
	thread that enumerates or captures (the reference ``setupESCAPI()``
	flow calls ``initCOM()`` before ``countCaptureDevices()``).  Skipping
	it makes ``countCaptureDevices()`` return 0 on systems where COM is
	not already initialised on that thread — notably on the background
	threads Luma captures from.
	"""
	if getattr(_com_initialized, "done", False):
		return
	try:
		dll.initCOM()
	except Exception:
		log.exception("ESCAPI initCOM failed")
	_com_initialized.done = True


# Resolutions tried in order by ``capture_camera``.  Some older webcams
# fail to initialise at 720p, so fall back to VGA before giving up.
_CAPTURE_RESOLUTIONS = ((1280, 720), (640, 480))

# Number of frames grabbed per capture; only the last one is kept.
# The first frame from a cold camera is often dark or grey because
# auto-exposure has not settled yet.
_WARMUP_FRAMES = 3


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_camera_available() -> bool:
	"""Check whether at least one webcam is detected.

	This is a fast, non-blocking call safe for the main thread.
	"""
	dll = _load_escapi()
	if dll is None:
		return False
	try:
		_ensure_com(dll)
		return dll.countCaptureDevices() > 0
	except Exception:
		log.exception("Error checking camera availability")
		return False


def get_camera_names() -> list[str]:
	"""Return the names of all detected webcams, in device order.

	The list index of each name is the device number to pass to
	:func:`capture_camera`.  Returns an empty list when no camera is
	found or ESCAPI is unavailable.
	"""
	dll = _load_escapi()
	if dll is None:
		return []
	try:
		_ensure_com(dll)
		count = dll.countCaptureDevices()
		names: list[str] = []
		for device in range(count):
			buf = ctypes.create_string_buffer(256)
			dll.getCaptureDeviceName(device, buf, len(buf))
			try:
				name = buf.value.decode("mbcs", errors="replace").strip()
			except LookupError:  # non-Windows fallback (tests)
				name = buf.value.decode("utf-8", errors="replace").strip()
			# Translators: Fallback name for a webcam whose driver reports no name.
			# {number} is the 1-based device number.
			names.append(name or _("Camera {number}").format(number=device + 1))
		return names
	except Exception:
		log.exception("Error enumerating cameras")
		return []


def capture_camera(device: int = 0, width: int = 1280, height: int = 720) -> str | None:
	"""Capture a single frame from webcam *device* as base64 PNG.

	**Blocking** — takes 1-3 seconds.  Must be called from a background
	thread (via ``worker.run_in_background``).

	Returns the base64-encoded PNG string, or ``None`` on failure.
	"""
	dll = _load_escapi()
	if dll is None:
		return None

	_ensure_com(dll)
	if dll.countCaptureDevices() <= 0:
		log.warning("No camera devices found")
		return None

	# Try the requested resolution first, then the fallbacks — some
	# older cameras refuse to initialise at 720p.
	resolutions = [(width, height)]
	resolutions += [r for r in _CAPTURE_RESOLUTIONS if r != (width, height)]
	for res_w, res_h in resolutions:
		result = _capture_at_resolution(dll, device, res_w, res_h)
		if result is not None:
			return result
	return None


def _capture_at_resolution(dll, device: int, width: int, height: int) -> str | None:
	"""Capture one frame at a fixed resolution; ``None`` on failure."""
	# Allocate BGRA pixel buffer (4 bytes per pixel stored as c_int).
	pixel_count = width * height
	buf = (ctypes.c_int * pixel_count)()

	params = _SimpleCapParams()
	params.buf = buf
	params.width = width
	params.height = height
	params.fps = 0.0  # Not used for single capture.

	if dll.initCapture(device, ctypes.byref(params)) == 0:
		log.error(
			"ESCAPI initCapture failed for device %d at %dx%d",
			device, width, height,
		)
		return None

	try:
		# Grab a few frames so auto-exposure can settle; keep the last.
		for frame in range(_WARMUP_FRAMES):
			dll.doCapture(device)

			# Poll until capture is done, with a 5-second timeout.
			deadline = time.monotonic() + 5.0
			while not dll.isCaptureDone(device):
				if time.monotonic() > deadline:
					log.error("Camera capture timed out")
					return None
				time.sleep(0.05)

		# Convert BGRA buffer → wx.Image → PNG → base64.
		return _buffer_to_base64(buf, width, height)
	finally:
		dll.deinitCapture(device)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _buffer_to_base64(buf, width: int, height: int) -> str | None:
	"""Convert a BGRA pixel buffer to a base64-encoded PNG string."""
	try:
		import wx

		# Build RGB byte array from BGRA int buffer.
		rgb = bytearray(width * height * 3)
		idx = 0
		for pixel in buf:
			# Each c_int is 0xAARRGGBB (little-endian BGRA).
			b = pixel & 0xFF
			g = (pixel >> 8) & 0xFF
			r = (pixel >> 16) & 0xFF
			rgb[idx] = r
			rgb[idx + 1] = g
			rgb[idx + 2] = b
			idx += 3

		image = wx.Image(width, height, bytes(rgb))
		stream = io.BytesIO()
		image.SaveFile(stream, wx.BITMAP_TYPE_PNG)
		data = stream.getvalue()
		if not data:
			return None
		return base64.b64encode(data).decode("ascii")
	except Exception:
		log.exception("Failed to convert camera buffer to PNG")
		return None
