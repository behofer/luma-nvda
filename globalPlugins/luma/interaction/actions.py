"""OS-level action execution for Interaction Mode.

Translates provider-agnostic action dicts (click, type, key, scroll,
etc.) into real mouse and keyboard events via ``ctypes``.

All coordinate actions are scaled from the provider's coordinate space
to real screen pixels through :mod:`interaction.scaling`.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import time

log = logging.getLogger(__name__)

from .scaling import DEFAULT_DISPLAY_HEIGHT, DEFAULT_DISPLAY_WIDTH, scale_to_screen

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

_user32 = ctypes.windll.user32

# mouse_event flags
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_HWHEEL = 0x1000
_WHEEL_DELTA = 120

# SendInput structures
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004


class _KEYBDINPUT(ctypes.Structure):
	_fields_ = [
		("wVk", ctypes.wintypes.WORD),
		("wScan", ctypes.wintypes.WORD),
		("dwFlags", ctypes.wintypes.DWORD),
		("time", ctypes.wintypes.DWORD),
		("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
	]


class _INPUT(ctypes.Structure):
	class _U(ctypes.Union):
		_fields_ = [("ki", _KEYBDINPUT)]

	_anonymous_ = ("_u",)
	_fields_ = [
		("type", ctypes.wintypes.DWORD),
		("_u", _U),
	]


# Virtual key code map for key names (Anthropic + Gemini conventions).
_VK_MAP: dict[str, int] = {
	"return": 0x0D, "enter": 0x0D,
	"tab": 0x09,
	"backspace": 0x08,
	"escape": 0x1B, "esc": 0x1B,
	"space": 0x20,
	"delete": 0x2E, "del": 0x2E,
	"home": 0x24,
	"end": 0x23,
	"page_up": 0x21, "pageup": 0x21,
	"page_down": 0x22, "pagedown": 0x22,
	"up": 0x26,
	"down": 0x28,
	"left": 0x25,
	"right": 0x27,
	"insert": 0x2D,
	"f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
	"f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
	"f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
	# Modifiers (used for combos).
	"ctrl": 0x11, "control": 0x11,
	"shift": 0x10,
	"alt": 0x12, "menu": 0x12,
	"super": 0x5B, "win": 0x5B, "meta": 0x5B,
}


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------

def execute_action(
	action: dict,
	screen_region: tuple[int, int, int, int],
	*,
	coordinate_size: tuple[int, int] = (DEFAULT_DISPLAY_WIDTH, DEFAULT_DISPLAY_HEIGHT),
) -> str:
	"""Execute a Computer Use action and return a description string.

	Parameters
	----------
	action:
		A common action dict, e.g.
		``{"action": "left_click", "coordinate": [500, 300]}``.
	screen_region:
		``(left, top, width, height)`` of the captured screen area.
	coordinate_size:
		``(api_width, api_height)`` — the provider's coordinate space.

	Returns
	-------
	A human-readable description of the executed action for speech
	feedback, e.g. ``"Clicked at 640, 480"``.
	"""
	name = action.get("action", "")
	try:
		if name == "screenshot":
			return _do_screenshot()
		if name == "left_click":
			return _do_click(action, screen_region, "left", coordinate_size)
		if name == "right_click":
			return _do_click(action, screen_region, "right", coordinate_size)
		if name == "middle_click":
			return _do_click(action, screen_region, "middle", coordinate_size)
		if name == "double_click":
			return _do_double_click(action, screen_region, coordinate_size)
		if name == "triple_click":
			return _do_triple_click(action, screen_region, coordinate_size)
		if name == "type":
			return _do_type(action)
		if name == "key":
			return _do_key(action)
		if name == "mouse_move":
			return _do_mouse_move(action, screen_region, coordinate_size)
		if name == "scroll":
			return _do_scroll(action, screen_region, coordinate_size)
		if name == "left_click_drag":
			return _do_left_click_drag(action, screen_region, coordinate_size)
		if name == "wait":
			return _do_wait(action)
		# Gemini-specific compound actions.
		if name == "type_at":
			return _do_type_at(action, screen_region, coordinate_size)
		if name == "scroll_page":
			return _do_scroll_page(action, screen_region)
		log.warning("Unknown action: %s", name)
		return f"Unknown action: {name}"
	except Exception:
		log.exception("Failed to execute action: %s", name)
		return f"Failed to execute: {name}"


# ---------------------------------------------------------------------------
# Action implementations
# ---------------------------------------------------------------------------

def _do_screenshot() -> str:
	# No-op — the loop captures a fresh screenshot after every step.
	return "Screenshot requested"


def _coords(
	action: dict,
	screen_region: tuple[int, int, int, int],
	coordinate_size: tuple[int, int],
) -> tuple[int, int]:
	"""Extract and scale coordinates from an action dict."""
	coord = action["coordinate"]
	return scale_to_screen(
		int(coord[0]), int(coord[1]), screen_region,
		coordinate_size=coordinate_size,
	)


def _move_and_click(x: int, y: int, down_flag: int, up_flag: int) -> None:
	_user32.SetCursorPos(x, y)
	time.sleep(0.02)
	_user32.mouse_event(down_flag, 0, 0, 0, 0)
	_user32.mouse_event(up_flag, 0, 0, 0, 0)


def _do_click(
	action: dict,
	screen_region: tuple[int, int, int, int],
	button: str,
	coordinate_size: tuple[int, int],
) -> str:
	x, y = _coords(action, screen_region, coordinate_size)
	if button == "left":
		_move_and_click(x, y, _MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP)
	elif button == "right":
		_move_and_click(x, y, _MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP)
	elif button == "middle":
		_move_and_click(x, y, _MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP)
	return f"{button.capitalize()} click at {x}, {y}"


def _do_double_click(
	action: dict,
	screen_region: tuple[int, int, int, int],
	coordinate_size: tuple[int, int],
) -> str:
	x, y = _coords(action, screen_region, coordinate_size)
	_user32.SetCursorPos(x, y)
	time.sleep(0.02)
	_user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
	_user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
	time.sleep(0.05)
	_user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
	_user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
	return f"Double click at {x}, {y}"


def _do_triple_click(
	action: dict,
	screen_region: tuple[int, int, int, int],
	coordinate_size: tuple[int, int],
) -> str:
	x, y = _coords(action, screen_region, coordinate_size)
	_user32.SetCursorPos(x, y)
	time.sleep(0.02)
	for _ in range(3):
		_user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
		_user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
		time.sleep(0.05)
	return f"Triple click at {x}, {y}"


def _do_type(action: dict) -> str:
	text = action.get("text", "")
	for ch in text:
		_send_unicode_char(ch)
		time.sleep(0.005)
	preview = text if len(text) <= 40 else text[:37] + "..."
	return f"Typed: {preview}"


def _send_unicode_char(char: str) -> None:
	"""Send a single Unicode character via SendInput."""
	code = ord(char)
	inputs = (_INPUT * 2)()

	# Key down
	inputs[0].type = _INPUT_KEYBOARD
	inputs[0].ki.wVk = 0
	inputs[0].ki.wScan = code
	inputs[0].ki.dwFlags = _KEYEVENTF_UNICODE
	inputs[0].ki.time = 0
	inputs[0].ki.dwExtraInfo = None

	# Key up
	inputs[1].type = _INPUT_KEYBOARD
	inputs[1].ki.wVk = 0
	inputs[1].ki.wScan = code
	inputs[1].ki.dwFlags = _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP
	inputs[1].ki.time = 0
	inputs[1].ki.dwExtraInfo = None

	_user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(_INPUT))


def _do_key(action: dict) -> str:
	"""Execute a key combo like ``ctrl+s``, ``alt+tab``, ``Return``."""
	combo = action.get("text", "")
	parts = [p.strip() for p in combo.split("+")]

	# Resolve each part to a VK code.
	vk_codes: list[int] = []
	for part in parts:
		vk = _VK_MAP.get(part.lower())
		if vk is None and len(part) == 1:
			# Single printable character — use VkKeyScanW.
			vk = _user32.VkKeyScanW(ord(part)) & 0xFF
		if vk is None:
			log.warning("Unknown key name: %s", part)
			return f"Unknown key: {part}"
		vk_codes.append(vk)

	# Press all keys down, then release in reverse order.
	for vk in vk_codes:
		_send_vk(vk, up=False)
		time.sleep(0.01)
	for vk in reversed(vk_codes):
		_send_vk(vk, up=True)
		time.sleep(0.01)

	return f"Key: {combo}"


def _send_vk(vk: int, *, up: bool = False) -> None:
	"""Send a single virtual-key press or release via SendInput."""
	inp = _INPUT()
	inp.type = _INPUT_KEYBOARD
	inp.ki.wVk = vk
	inp.ki.wScan = 0
	inp.ki.dwFlags = _KEYEVENTF_KEYUP if up else 0
	inp.ki.time = 0
	inp.ki.dwExtraInfo = None
	_user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _do_mouse_move(
	action: dict,
	screen_region: tuple[int, int, int, int],
	coordinate_size: tuple[int, int],
) -> str:
	x, y = _coords(action, screen_region, coordinate_size)
	_user32.SetCursorPos(x, y)
	return f"Mouse moved to {x}, {y}"


def _do_scroll(
	action: dict,
	screen_region: tuple[int, int, int, int],
	coordinate_size: tuple[int, int],
) -> str:
	x, y = _coords(action, screen_region, coordinate_size)
	direction = action.get("scroll_direction", "down")
	amount = int(action.get("scroll_amount", 3))
	_user32.SetCursorPos(x, y)
	time.sleep(0.02)

	if direction in ("up", "down"):
		delta = _WHEEL_DELTA * amount * (1 if direction == "up" else -1)
		_user32.mouse_event(_MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
	elif direction in ("left", "right"):
		delta = _WHEEL_DELTA * amount * (-1 if direction == "left" else 1)
		_user32.mouse_event(_MOUSEEVENTF_HWHEEL, 0, 0, delta, 0)

	return f"Scroll {direction} {amount} at {x}, {y}"


def _do_left_click_drag(
	action: dict,
	screen_region: tuple[int, int, int, int],
	coordinate_size: tuple[int, int],
) -> str:
	start = action["coordinate"]
	end = action["target_coordinate"]
	sx, sy = scale_to_screen(
		int(start[0]), int(start[1]), screen_region,
		coordinate_size=coordinate_size,
	)
	ex, ey = scale_to_screen(
		int(end[0]), int(end[1]), screen_region,
		coordinate_size=coordinate_size,
	)

	_user32.SetCursorPos(sx, sy)
	time.sleep(0.02)
	_user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
	time.sleep(0.1)
	_user32.SetCursorPos(ex, ey)
	time.sleep(0.02)
	_user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

	return f"Drag from {sx},{sy} to {ex},{ey}"


def _do_wait(action: dict) -> str:
	duration = int(action.get("duration", 1))
	time.sleep(duration)
	return f"Waited {duration}s"


# ---------------------------------------------------------------------------
# Gemini compound actions
# ---------------------------------------------------------------------------

def _do_type_at(
	action: dict,
	screen_region: tuple[int, int, int, int],
	coordinate_size: tuple[int, int],
) -> str:
	"""Click at a position, optionally clear, type text, optionally press Enter."""
	x, y = _coords(action, screen_region, coordinate_size)
	_move_and_click(x, y, _MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP)
	time.sleep(0.1)

	if action.get("clear_before_typing"):
		_do_key({"text": "ctrl+a"})
		time.sleep(0.05)
		_do_key({"text": "delete"})
		time.sleep(0.05)

	text = action.get("text", "")
	for ch in text:
		_send_unicode_char(ch)
		time.sleep(0.005)

	if action.get("press_enter"):
		time.sleep(0.05)
		_do_key({"text": "Return"})

	preview = text if len(text) <= 30 else text[:27] + "..."
	return f"Typed at {x},{y}: {preview}"


def _do_scroll_page(
	action: dict,
	screen_region: tuple[int, int, int, int],
) -> str:
	"""Scroll the page without specific coordinates (scroll at screen centre)."""
	left, top, width, height = screen_region
	cx = left + width // 2
	cy = top + height // 2
	direction = action.get("scroll_direction", "down")
	amount = 3

	_user32.SetCursorPos(cx, cy)
	time.sleep(0.02)

	if direction in ("up", "down"):
		delta = _WHEEL_DELTA * amount * (1 if direction == "up" else -1)
		_user32.mouse_event(_MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
	elif direction in ("left", "right"):
		delta = _WHEEL_DELTA * amount * (-1 if direction == "left" else 1)
		_user32.mouse_event(_MOUSEEVENTF_HWHEEL, 0, 0, delta, 0)

	return f"Scroll page {direction}"
