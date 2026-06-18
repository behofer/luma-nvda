"""Screen and navigator object image capture for Luma.

Provides functions to capture the entire screen or a specific NVDA
navigator object as a base64-encoded PNG image, suitable for sending
to vision-capable LLMs.

All capture functions use wxPython's screen DC and are safe to call
from the main thread (they are fast, non-blocking GDI operations).
"""

from __future__ import annotations

import base64
import ctypes
import io
import logging

import wx

log = logging.getLogger(__name__)

# Image optimisation defaults — applied when "send_full_images" is off.
_MAX_IMAGE_DIM = 1536
_JPEG_QUALITY = 90

# Win32 constants for ShowWindow.
_SW_MAXIMIZE = 3

_user32 = ctypes.windll.user32


def capture_screen() -> str | None:
	"""Capture the entire virtual screen as a base64-encoded PNG string.

	Returns ``None`` if the capture fails for any reason.
	"""
	try:
		screen_dc = wx.ScreenDC()
		w, h = wx.DisplaySize()
		bmp = wx.Bitmap(w, h)
		mem_dc = wx.MemoryDC(bmp)
		mem_dc.Blit(0, 0, w, h, screen_dc, 0, 0)
		mem_dc.SelectObject(wx.NullBitmap)
		return _bitmap_to_base64(bmp)
	except Exception:
		log.exception("Failed to capture screen")
		return None


def capture_object(nav_object) -> str | None:
	"""Capture the bounding rectangle of an NVDA navigator object.

	*nav_object* should be an NVDA ``NVDAObject`` that exposes a
	``location`` attribute (a ``RectLTWH`` or compatible tuple of
	``(left, top, width, height)``).

	Falls back to a full-screen capture if the object has no location.
	Returns ``None`` on failure.
	"""
	location = getattr(nav_object, "location", None)
	if not location:
		log.debug("Navigator object has no location — falling back to full screen.")
		return capture_screen()

	try:
		left, top, width, height = location
	except (TypeError, ValueError):
		log.warning("Could not unpack navigator object location: %r", location)
		return capture_screen()

	if width <= 0 or height <= 0:
		log.debug("Navigator object has zero-area location — falling back to full screen.")
		return capture_screen()

	try:
		screen_dc = wx.ScreenDC()
		bmp = wx.Bitmap(width, height)
		mem_dc = wx.MemoryDC(bmp)
		mem_dc.Blit(0, 0, width, height, screen_dc, left, top)
		mem_dc.SelectObject(wx.NullBitmap)
		return _bitmap_to_base64(bmp)
	except Exception:
		log.exception("Failed to capture navigator object at %r", location)
		return None


def capture_app() -> str | None:
	"""Capture the foreground application's window as a base64-encoded PNG.

	The window is maximized before capturing so the AI receives
	the fullest possible view of the application.  Uses the NVDA
	``api`` module to find the foreground object's location.
	Falls back to full-screen capture if unavailable.
	"""
	try:
		import api as nvda_api
		fg = nvda_api.getForegroundObject()
	except Exception:
		log.debug("Could not get foreground object — falling back to full screen.")
		return capture_screen()

	if fg is None:
		return capture_screen()

	_ensure_maximized(fg)
	return capture_object(fg)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def get_screen_region() -> tuple[int, int, int, int]:
	"""Return ``(left, top, width, height)`` for the full virtual screen.

	Used by the interaction feature to translate VLM-provided normalised
	coordinates back to absolute screen pixels.
	"""
	w, h = wx.DisplaySize()
	return (0, 0, w, h)


def get_app_region() -> tuple[int, int, int, int] | None:
	"""Return ``(left, top, width, height)`` for the foreground application window.

	The window is maximized first (same as :func:`capture_app`) so
	the region matches the captured image.  Returns ``None`` if the
	foreground window's location cannot be determined.
	"""
	try:
		import api as nvda_api
		fg = nvda_api.getForegroundObject()
	except Exception:
		return None
	if fg is None:
		return None
	_ensure_maximized(fg)
	loc = getattr(fg, "location", None)
	if not loc:
		return None
	try:
		left, top, width, height = loc
		if width <= 0 or height <= 0:
			return None
		return (left, top, width, height)
	except (TypeError, ValueError):
		return None


def _ensure_maximized(nav_object) -> None:
	"""Maximize the window of *nav_object* if it is not already maximized.

	Uses Win32 ``ShowWindow(hwnd, SW_MAXIMIZE)`` and pauses briefly
	to let the window repaint at the new size before a GDI capture.
	"""
	hwnd = getattr(nav_object, "windowHandle", None) or 0
	if not hwnd:
		return
	if _user32.IsZoomed(hwnd):
		return
	_user32.ShowWindow(hwnd, _SW_MAXIMIZE)
	# Brief pause so the window finishes resizing and repainting.
	wx.MilliSleep(150)


def optimize_image(image_base64: str) -> str:
	"""Downscale and JPEG-compress a base64 image for smaller payloads.

	When the ``send_full_images`` setting is off (the default), the
	image is decoded, downscaled so its longest side does not exceed
	``_MAX_IMAGE_DIM``, and re-encoded as JPEG.  This dramatically
	reduces payload size — from several MB to ~100-200 KB — which
	speeds up cloud uploads and reduces memory pressure on local
	models.

	When the setting is on, the image is returned unchanged.

	Safe to call from any thread.
	"""
	from . import config as luma_config

	if luma_config.get_setting("send_full_images", False):
		return image_base64

	try:
		raw = base64.b64decode(image_base64)
		stream_in = io.BytesIO(raw)
		image = wx.Image(stream_in, wx.BITMAP_TYPE_ANY)
		if not image.IsOk():
			return image_base64

		w, h = image.GetWidth(), image.GetHeight()
		if max(w, h) > _MAX_IMAGE_DIM:
			scale = _MAX_IMAGE_DIM / max(w, h)
			new_w = max(1, int(w * scale))
			new_h = max(1, int(h * scale))
			image = image.Scale(new_w, new_h, wx.IMAGE_QUALITY_HIGH)

		image.SetOption(wx.IMAGE_OPTION_QUALITY, str(_JPEG_QUALITY))
		stream_out = io.BytesIO()
		image.SaveFile(stream_out, wx.BITMAP_TYPE_JPEG)
		data = stream_out.getvalue()
		if not data:
			return image_base64
		return base64.b64encode(data).decode("ascii")
	except Exception:
		log.debug("Image optimisation failed — sending original.", exc_info=True)
		return image_base64


def _bitmap_to_base64(bmp: wx.Bitmap) -> str | None:
	"""Convert a ``wx.Bitmap`` to a base64-encoded PNG string."""
	image = bmp.ConvertToImage()
	stream = io.BytesIO()
	image.SaveFile(stream, wx.BITMAP_TYPE_PNG)
	data = stream.getvalue()
	if not data:
		return None
	return base64.b64encode(data).decode("ascii")
