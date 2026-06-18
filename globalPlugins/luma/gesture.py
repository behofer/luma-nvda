"""Gesture infrastructure for Luma.

Provides :class:`DoublePress` for consistent double-press detection
across all Luma gestures, and the canonical gesture binding table.
"""

from __future__ import annotations

import wx


# Threshold in seconds for double-press detection.
DOUBLE_PRESS_THRESHOLD: float = 0.4


class DoublePress:
	"""Encapsulates double-press detection for a single gesture.

	Call :meth:`tap` on each key press.  After the threshold window
	closes, *handler* is called with the accumulated tap count
	(1 for single press, >= 2 for double press).

	Example inside ``GlobalPlugin``::

		self._screen = DoublePress(self._on_screen_tap)

		def script_processScreen(self, gesture):
			self._screen.tap()

		def _on_screen_tap(self, count):
			if count >= 2:
				...  # double-press action
			else:
				...  # single-press action
	"""

	def __init__(
		self,
		handler,
		threshold: float = DOUBLE_PRESS_THRESHOLD,
	) -> None:
		self._handler = handler
		self._threshold = threshold
		self._count: int = 0

	def tap(self) -> None:
		"""Register one key press.  Starts the timer on the first press."""
		self._count += 1
		if self._count == 1:
			wx.CallLater(int(self._threshold * 1000), self._fire)

	def _fire(self) -> None:
		"""Called when the threshold window expires."""
		count = self._count
		self._count = 0
		self._handler(count)


# -------------------------------------------------------------------
# Canonical gesture -> script mapping.
#
# Values are script method names *without* the ``script_`` prefix.
# ``KB:`` (uppercase) is used for vision / double-press gestures per
# NVDA convention; ``kb:`` for menu and utility gestures.
# -------------------------------------------------------------------

GESTURE_MAP: dict[str, str] = {
	# Menu / utility
	"kb:shift+NVDA+enter": "lumaMenu",
	"kb:shift+NVDA+t": "textChat",
	"kb:shift+NVDA+i": "interactionMode",
	"kb:shift+NVDA+k": "toggleLiveMode",
	"kb:shift+NVDA+F4": "stopProcessing",
	"kb:shift+NVDA+space": "toggleActiveSkill",
	# Vision / double-press
	"KB:shift+NVDA+r": "processNavigator",
	"KB:shift+NVDA+g": "processScreen",
	"KB:shift+NVDA+a": "processApp",
	"KB:shift+NVDA+c": "processClipboard",
	"KB:shift+NVDA+l": "processCamera",
	# Video analysis
	"kb:shift+NVDA+v": "processVideo",
	# File rename
	"kb:shift+NVDA+f2": "renameFiles",
	# Custom shortcuts (1-9, 0)
	"kb:shift+NVDA+1": "shortcut1",
	"kb:shift+NVDA+2": "shortcut2",
	"kb:shift+NVDA+3": "shortcut3",
	"kb:shift+NVDA+4": "shortcut4",
	"kb:shift+NVDA+5": "shortcut5",
	"kb:shift+NVDA+6": "shortcut6",
	"kb:shift+NVDA+7": "shortcut7",
	"kb:shift+NVDA+8": "shortcut8",
	"kb:shift+NVDA+9": "shortcut9",
	"kb:shift+NVDA+0": "shortcut0",
}
