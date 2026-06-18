"""OS-level interaction executor for Luma.

Performs mouse clicks at absolute screen coordinates, translating from
the VLM's normalised 0-1000 coordinate space back to real pixels.

All functions in this module MUST be called on the main thread (via
``wx.CallAfter``) as they interact with the OS UI.
"""

from __future__ import annotations

import ctypes
import logging

import ui

log = logging.getLogger(__name__)

# Windows API constants for mouse_event.
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004


def perform_click(
	norm_x: int,
	norm_y: int,
	screen_region: tuple[int, int, int, int],
) -> None:
	"""Move the mouse cursor and left-click at the translated position.

	Parameters
	----------
	norm_x, norm_y:
		Coordinates on a 0-1000 scale (from the VLM's ``data-coord``).
	screen_region:
		``(left, top, width, height)`` of the captured screen region.
		The normalised coordinates are mapped into this rectangle.
	"""
	left, top, width, height = screen_region
	# Clamp normalised coordinates to the valid 0-1000 range so a
	# misbehaving VLM cannot trigger clicks at arbitrary locations.
	norm_x = max(0, min(1000, norm_x))
	norm_y = max(0, min(1000, norm_y))
	target_x = int(left + (norm_x / 1000) * width)
	target_y = int(top + (norm_y / 1000) * height)

	log.info(
		"Performing click at (%d, %d) from normalised (%d, %d), region %r",
		target_x, target_y, norm_x, norm_y, screen_region,
	)

	# Translators: Spoken when Luma clicks at a screen position.
	# {x} and {y} are the pixel coordinates.
	ui.message(_("Clicking at {x}, {y}").format(x=target_x, y=target_y))

	try:
		ctypes.windll.user32.SetCursorPos(target_x, target_y)
		ctypes.windll.user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
		ctypes.windll.user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
	except Exception:
		log.exception("Failed to perform click at (%d, %d)", target_x, target_y)
		# Translators: Spoken when a simulated click fails.
		ui.message(_("Failed to perform click."))
