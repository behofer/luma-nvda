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
	"""Capture exactly the on-screen area of an NVDA navigator object.

	*nav_object* should be an NVDA ``NVDAObject``.  Its bounding rectangle
	is resolved by :func:`get_object_rect`, which also handles objects that
	report no (or a degenerate) location of their own — e.g. a link wrapping
	an image in Chromium — by using the extent of their descendants.

	Never falls back to a full-screen capture: sending the whole screen when
	the user asked about one object is misleading.  Returns ``None`` if no
	visible rectangle can be resolved or the capture fails.
	"""
	rect = get_object_rect(nav_object)
	if rect is None:
		log.debug("Navigator object has no visible location: %r", nav_object)
		return None
	return _capture_rect(rect)


def get_object_rect(nav_object) -> tuple[int, int, int, int] | None:
	"""Return the visible ``(left, top, width, height)`` of *nav_object*.

	Uses the object's own location when it has a positive area.  Otherwise
	(Chromium reports e.g. an inline ``<a>`` containing an image with zero
	width or height) the union of its descendants' locations is used, which
	is the area the object visually covers.  The result is clipped to the
	virtual screen; ``None`` means nothing visible could be resolved.
	"""
	rect = _location_of(nav_object)
	if rect is None:
		rect = _descendants_union(nav_object)
		if rect is not None:
			log.debug(
				"Navigator object has no usable location — using descendants' extent %r.",
				rect,
			)
	if rect is None:
		return None
	return _clip_to_virtual_screen(rect)


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
	rect = get_object_rect(fg)
	if rect is None:
		log.debug("Foreground window has no location — falling back to full screen.")
		return capture_screen()
	return _capture_rect(rect)


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


# Bounds for the descendant walk in :func:`_descendants_union`.  Keeps the
# walk fast on the main thread even for objects with large subtrees.
_MAX_DESCENDANT_DEPTH = 6
_MAX_DESCENDANTS = 200

# GetSystemMetrics indices for the virtual screen (all monitors).
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


def _location_of(obj) -> tuple[int, int, int, int] | None:
	"""Return *obj*'s location as an ``(l, t, w, h)`` tuple if it has a positive area."""
	try:
		location = obj.location
	except Exception:
		return None
	if not location:
		return None
	try:
		left, top, width, height = location
	except (TypeError, ValueError):
		log.warning("Could not unpack object location: %r", location)
		return None
	if width <= 0 or height <= 0:
		return None
	return (int(left), int(top), int(width), int(height))


def _descendants_union(obj) -> tuple[int, int, int, int] | None:
	"""Return the bounding box of all descendants of *obj* that have a location.

	Breadth-first, bounded by ``_MAX_DESCENDANT_DEPTH`` and
	``_MAX_DESCENDANTS``.  A descendant with its own location is not
	descended into further (its rectangle already covers its children).
	"""
	rects = []
	visited = 0
	level = [obj]
	for _depth in range(_MAX_DESCENDANT_DEPTH):
		next_level = []
		for parent in level:
			try:
				child = parent.firstChild
			except Exception:
				child = None
			while child is not None and visited < _MAX_DESCENDANTS:
				visited += 1
				rect = _location_of(child)
				if rect is not None:
					rects.append(rect)
				else:
					next_level.append(child)
				try:
					child = child.next
				except Exception:
					child = None
		if not next_level or visited >= _MAX_DESCENDANTS:
			break
		level = next_level
	if not rects:
		return None
	left = min(r[0] for r in rects)
	top = min(r[1] for r in rects)
	right = max(r[0] + r[2] for r in rects)
	bottom = max(r[1] + r[3] for r in rects)
	return (left, top, right - left, bottom - top)


def _clip_to_virtual_screen(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
	"""Clip *rect* to the virtual screen; ``None`` if it lies entirely off-screen."""
	left, top, width, height = rect
	try:
		vs_left = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
		vs_top = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
		vs_right = vs_left + _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
		vs_bottom = vs_top + _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
	except Exception:
		return rect
	if vs_right <= vs_left or vs_bottom <= vs_top:
		return rect
	new_left = max(left, vs_left)
	new_top = max(top, vs_top)
	new_right = min(left + width, vs_right)
	new_bottom = min(top + height, vs_bottom)
	if new_right <= new_left or new_bottom <= new_top:
		return None
	return (new_left, new_top, new_right - new_left, new_bottom - new_top)


def _capture_rect(rect: tuple[int, int, int, int]) -> str | None:
	"""Capture the screen area *rect* ``(left, top, width, height)`` as base64 PNG."""
	left, top, width, height = rect
	try:
		screen_dc = wx.ScreenDC()
		bmp = wx.Bitmap(width, height)
		mem_dc = wx.MemoryDC(bmp)
		mem_dc.Blit(0, 0, width, height, screen_dc, left, top)
		mem_dc.SelectObject(wx.NullBitmap)
		return _bitmap_to_base64(bmp)
	except Exception:
		log.exception("Failed to capture screen area %r", rect)
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
