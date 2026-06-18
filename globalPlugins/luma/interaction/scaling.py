"""Screenshot scaling and coordinate translation for Interaction Mode.

Different providers use different coordinate systems:

- **Anthropic** operates in a declared pixel space (e.g. 1280x800).
  Screenshots are resized to that space and coordinates map 1:1.
- **Gemini** uses a normalised 0–999 grid regardless of image size.
  Screenshots are resized to a reasonable resolution and coordinates
  are divided by 1000 to get a 0.0–1.0 ratio.

The helper functions accept an explicit ``display_size`` /
``coordinate_size`` so the agent loop can pass provider-specific values.
"""

from __future__ import annotations

import base64
import io
import logging

import wx

log = logging.getLogger(__name__)

# Default display/coordinate size (Anthropic's recommended space).
DEFAULT_DISPLAY_WIDTH: int = 1280
DEFAULT_DISPLAY_HEIGHT: int = 800


def resize_screenshot(
	base64_png: str,
	*,
	display_size: tuple[int, int] = (DEFAULT_DISPLAY_WIDTH, DEFAULT_DISPLAY_HEIGHT),
) -> str:
	"""Resize a full-resolution base64 PNG to *display_size*.

	Returns a new base64-encoded PNG at the target dimensions.
	"""
	target_w, target_h = display_size
	raw = base64.b64decode(base64_png)
	stream_in = io.BytesIO(raw)
	image = wx.Image(stream_in, wx.BITMAP_TYPE_PNG)
	if not image.IsOk():
		log.warning("Failed to decode screenshot for resizing; returning original.")
		return base64_png

	image = image.Scale(target_w, target_h, wx.IMAGE_QUALITY_HIGH)

	stream_out = io.BytesIO()
	image.SaveFile(stream_out, wx.BITMAP_TYPE_PNG)
	return base64.b64encode(stream_out.getvalue()).decode("ascii")


def scale_to_screen(
	x: int,
	y: int,
	screen_region: tuple[int, int, int, int],
	*,
	coordinate_size: tuple[int, int] = (DEFAULT_DISPLAY_WIDTH, DEFAULT_DISPLAY_HEIGHT),
) -> tuple[int, int]:
	"""Map coordinates from the API's space to real screen pixels.

	Parameters
	----------
	x, y:
		Coordinates in the API's coordinate space.
	screen_region:
		``(left, top, width, height)`` of the captured screen area.
	coordinate_size:
		``(api_width, api_height)`` — the size of the coordinate space
		used by the provider.  For Anthropic this matches the display
		size (e.g. 1280x800); for Gemini it is 1000x1000 (normalised
		0–999 grid).

	Returns
	-------
	``(real_x, real_y)`` in absolute screen pixels.
	"""
	left, top, width, height = screen_region
	api_w, api_h = coordinate_size
	real_x = int(left + (x / api_w) * width)
	real_y = int(top + (y / api_h) * height)
	return real_x, real_y
