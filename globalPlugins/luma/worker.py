"""Asynchronous worker for Luma API requests.

Ensures that all network-bound operations (model fetching, chat
completions) run in background threads so the main NVDA thread is
never blocked.  Results and errors are dispatched back to the main
thread via ``wx.CallAfter``.

A looping processing sound (``assets/processing.wav``) plays while
any background task is active, giving the user auditory feedback.
"""

from __future__ import annotations

import logging
import os
import threading
import winsound
from typing import Any, Callable

import wx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Processing indicator sound
# ---------------------------------------------------------------------------

# Resolve the WAV path once at import time.
_SOUND_PATH: str = os.path.normpath(
	os.path.join(os.path.dirname(__file__), "..", "..", "assets", "processing.wav")
)

# How often (ms) the processing sound repeats.
_SOUND_INTERVAL_MS: int = 800

# Reference counter for concurrent background tasks (main-thread only).
_processing_count: int = 0
_processing_timer: wx.CallLater | None = None


def _start_processing_sound() -> None:
	"""Increment the task counter and start the sound loop if needed."""
	global _processing_count
	_processing_count += 1
	if _processing_count == 1:
		_play_tick()


def _stop_processing_sound() -> None:
	"""Decrement the task counter; silence the loop when no tasks remain."""
	global _processing_count, _processing_timer
	_processing_count = max(0, _processing_count - 1)
	if _processing_count == 0 and _processing_timer is not None:
		_processing_timer.Stop()
		_processing_timer = None


def force_stop_sound() -> None:
	"""Immediately silence the processing sound, resetting the counter.

	Called from the stop-processing gesture so the user gets instant
	auditory feedback that the operation was cancelled.  Must be called
	on the **main thread**.
	"""
	global _processing_count, _processing_timer
	_processing_count = 0
	if _processing_timer is not None:
		_processing_timer.Stop()
		_processing_timer = None


def _play_tick() -> None:
	"""Play the processing sound once and schedule the next tick."""
	global _processing_timer
	if _processing_count <= 0:
		_processing_timer = None
		return
	try:
		winsound.PlaySound(
			_SOUND_PATH,
			winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
		)
	except Exception:
		log.debug("Could not play processing sound", exc_info=True)
	_processing_timer = wx.CallLater(_SOUND_INTERVAL_MS, _play_tick)


def run_in_background(
	target: Callable[..., Any],
	*,
	args: tuple = (),
	kwargs: dict | None = None,
	on_success: Callable[[Any], None] | None = None,
	on_error: Callable[[Exception], None] | None = None,
	silent: bool = False,
	cancel_event: threading.Event | None = None,
) -> threading.Thread:
	"""Run *target* in a daemon thread with main-thread callbacks.

	Parameters
	----------
	target:
		The callable to execute in the background (e.g. an API call).
	args / kwargs:
		Positional and keyword arguments forwarded to *target*.
	on_success:
		Called on the **main thread** (via ``wx.CallAfter``) with the
		return value of *target* when it completes without error.
	on_error:
		Called on the **main thread** with the caught ``Exception``
		if *target* raises.
	silent:
		If ``True``, suppress the looping processing sound.  Useful
		for tasks that provide their own auditory feedback (e.g.
		voice recording).
	cancel_event:
		Optional :class:`threading.Event`.  When set, the task's
		``on_success`` / ``on_error`` callbacks are suppressed once
		the target finishes.  The target itself still runs to
		completion (HTTP requests cannot be interrupted), but
		the result is silently discarded.

	Returns the started :class:`threading.Thread` (for testing /
	cancellation reference, though the thread is a daemon).
	"""
	if kwargs is None:
		kwargs = {}

	def _wrapper() -> None:
		if not silent:
			wx.CallAfter(_start_processing_sound)
		try:
			result = target(*args, **kwargs)
		except Exception as exc:
			log.exception("Background task %s failed", target.__name__)
			if not silent:
				wx.CallAfter(_stop_processing_sound)
			if cancel_event is not None and cancel_event.is_set():
				return  # Cancelled — suppress callback.
			if on_error is not None:
				wx.CallAfter(on_error, exc)
		else:
			if not silent:
				wx.CallAfter(_stop_processing_sound)
			if cancel_event is not None and cancel_event.is_set():
				return  # Cancelled — suppress callback.
			if on_success is not None:
				wx.CallAfter(on_success, result)

	thread = threading.Thread(target=_wrapper, daemon=True)
	thread.start()
	return thread


def process_skill(
	provider,
	*,
	model: str,
	prompt: str,
	image_base64: str | None = None,
	temperature: float = 0.3,
	timeout: int = 60,
	on_success: Callable[[str], None] | None = None,
	on_error: Callable[[Exception], None] | None = None,
	cancel_event: threading.Event | None = None,
) -> threading.Thread:
	"""High-level helper: send a skill request in the background.

	Builds the message list, calls :meth:`provider.process_request`,
	and dispatches the result string back to the main thread.

	Image optimisation (downscale + JPEG) is applied in the background
	thread so the main NVDA thread stays responsive.
	"""
	messages = [{"role": "user", "content": prompt}]

	def _do_request() -> str:
		img = image_base64
		if img:
			from .capture import optimize_image
			img = optimize_image(img)
		return provider.process_request(
			model=model,
			messages=messages,
			temperature=temperature,
			image_base64=img,
			timeout=timeout,
		)

	return run_in_background(
		_do_request,
		on_success=on_success,
		on_error=on_error,
		cancel_event=cancel_event,
	)
