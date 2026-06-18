"""Webcam camera capture for Luma.

Provides functions to capture a single frame from the default webcam
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
import time

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ESCAPI ctypes bindings
# ---------------------------------------------------------------------------

# Path to the bundled ESCAPI 64-bit DLL.
_DLL_PATH = os.path.join(os.path.dirname(__file__), "lib", "escapi.dll")

# Lazily loaded DLL handle.
_escapi = None


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

	# int countCaptureDevices()
	dll.countCaptureDevices.restype = ctypes.c_int
	dll.countCaptureDevices.argtypes = []

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
		return dll.countCaptureDevices() > 0
	except Exception:
		log.exception("Error checking camera availability")
		return False


def capture_camera(width: int = 1280, height: int = 720) -> str | None:
	"""Capture a single frame from the default webcam as base64 PNG.

	**Blocking** — takes 1-3 seconds.  Must be called from a background
	thread (via ``worker.run_in_background``).

	Returns the base64-encoded PNG string, or ``None`` on failure.
	"""
	dll = _load_escapi()
	if dll is None:
		return None

	if dll.countCaptureDevices() <= 0:
		log.warning("No camera devices found")
		return None

	# Allocate BGRA pixel buffer (4 bytes per pixel stored as c_int).
	pixel_count = width * height
	buf = (ctypes.c_int * pixel_count)()

	params = _SimpleCapParams()
	params.buf = buf
	params.width = width
	params.height = height
	params.fps = 0.0  # Not used for single capture.

	device = 0
	if dll.initCapture(device, ctypes.byref(params)) == 0:
		log.error("ESCAPI initCapture failed for device %d", device)
		return None

	try:
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
