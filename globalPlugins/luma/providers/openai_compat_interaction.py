"""OpenAI-compatible *Interaction Mode* provider for Luma.

Adds Computer Use to any OpenAI-compatible endpoint that serves a
vision-language model with GUI-grounding ability — vLLM, SGLang,
llama.cpp server, LM Studio, OpenRouter, … running e.g. **Qwen3-VL**,
Qwen2.5-VL, UI-TARS or similar.

Unlike Anthropic and Gemini there is no native "computer" tool, so
this provider implements the agent protocol itself:

* **Prompted tool protocol.**  A system prompt describes a single
  ``computer_use`` function (same action vocabulary as Anthropic and
  as the official Qwen computer-use recipe) and asks the model to
  answer with one short sentence of narration followed by
  ``<tool_call>{"name": "computer_use", "arguments": {...}}</tool_call>``.
  No server-side tool-calling support is required; if the server *does*
  parse tool calls (vLLM with ``--enable-auto-tool-choice``) the
  structured ``tool_calls`` field is used instead.
* **Configurable coordinate convention.**  ``coordinate_mode`` is either
  ``"normalized"`` (a 0–1000 grid — native for Qwen3-VL and Gemini) or
  ``"pixels"`` (absolute pixels of the screenshot — native for
  Qwen2.5-VL and UI-TARS).  In pixel mode the screenshot is sent with
  dimensions that are multiples of 28 so the server's ``smart_resize``
  is an identity and the model's pixels equal ours.
* **Aspect-preserving screenshots.**  The screen is scaled to fit a
  configurable long side (default 1280 px) without distortion, which
  open models ground much better on than a fixed 1280x800 squash.
* **History pruning.**  Only the most recent screenshots are kept as
  images in the conversation; older ones are replaced by a text
  placeholder so a 50-step session stays within a typical 32k context.
* **Lenient parsing.**  ``<think>`` blocks are stripped, ``<tool_call>``
  wrappers, fenced JSON and bare JSON objects are all accepted, and
  common action-name synonyms (``click``, ``hotkey``, ``finished`` …)
  are normalised to the common action format executed by
  :mod:`interaction.actions`.
"""

from __future__ import annotations

import json
import logging
import re

from .base import register_provider
from .openai_compat import OpenAICompatProvider

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Coordinate conventions understood by this provider.  ``"auto"`` picks
#: one from the model name at the start of each interaction session.
COORDINATE_MODES: tuple[str, ...] = ("auto", "normalized", "pixels")

#: Model-name fragments (lower-case) that imply absolute pixel coordinates.
_PIXEL_MODEL_HINTS: tuple[str, ...] = (
	"qwen2.5-vl", "qwen2_5-vl", "qwen2.5vl", "qwen2-vl", "qwen2vl",
	"ui-tars", "uitars", "ui_tars", "showui", "os-atlas", "aguvis",
)

#: Model-name fragments (lower-case) that imply a normalised 0-1000 grid.
_NORMALIZED_MODEL_HINTS: tuple[str, ...] = (
	"qwen3-vl", "qwen3vl", "qwen3_vl", "gemini", "glm-4.5v", "glm-4.6v", "glm4.5v",
	"gelato", "holo",
)

#: Default long side (px) of the screenshot sent to the model.  Full HD
#: is sent unscaled: GUI grounding degrades quickly when small controls
#: (taskbar icons, checkboxes) are shrunk.
DEFAULT_IMAGE_LONG_SIDE: int = 1920

#: Size (screen px) of the region cut around a proposed click for the
#: zoomed second look, and the enlargement factor.  448x280 x2 = 896x560,
#: both multiples of 28.
_REFINE_CROP_SIZE: tuple[int, int] = (448, 280)
_REFINE_ZOOM: int = 2

#: Seconds to wait after an action before taking the result screenshot.
_SETTLE_DELAY: float = 1.0

#: Screenshots are sent as JPEG: a full-HD desktop PNG is 2-4 MB of
#: base64, and three of them in the history exceeded a server's request
#: body limit ("Failed to buffer the request body: length limit exceeded").
#: JPEG at this quality is ~10x smaller and still perfectly legible.
_IMAGE_FORMAT: str = "jpeg"
_IMAGE_QUALITY: int = 85
_IMAGE_MIME: str = "image/jpeg"

#: Substrings (lower-case) in an HTTP error that mean "request too big".
_TOO_LARGE_HINTS: tuple[str, ...] = (
	"length limit", "too large", "entity too large", "payload too large",
	"413", "body limit", "exceeds the maximum", "request body",
)

#: Screenshot dimensions are rounded to a multiple of this so that the
#: Qwen-style ``smart_resize`` on the server leaves the image untouched.
_SIZE_FACTOR: int = 28

#: How many screenshots to keep as *images* in the message history.
#: Older ones are replaced with a short text placeholder.
_MAX_SCREENSHOTS_IN_HISTORY: int = 3

#: After this many consecutive responses without a parsable action the
#: loop is ended (the model is nudged once in between).
_MAX_NO_ACTION_RESPONSES: int = 2

#: Sampling temperature for agent turns — grounding wants determinism.
_AGENT_TEMPERATURE: float = 0.1

_SCREENSHOT_PLACEHOLDER = "[earlier screenshot removed to save context]"


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

@register_provider
class OpenAICompatInteractionProvider(OpenAICompatProvider):
	"""OpenAI-compatible provider with prompted Computer Use support."""

	def __init__(
		self,
		name: str,
		api_url: str,
		api_key: str = "",
		model_fetch_url: str = "",
		coordinate_mode: str = "auto",
		image_long_side: int = DEFAULT_IMAGE_LONG_SIDE,
		refine_clicks: bool = True,
	) -> None:
		super().__init__(
			name=name, api_url=api_url, api_key=api_key,
			model_fetch_url=model_fetch_url,
		)
		self._refine_clicks = bool(refine_clicks)
		self._coordinate_mode = (
			coordinate_mode if coordinate_mode in COORDINATE_MODES else "auto"
		)
		# Mode actually in use for the current session ("normalized"/"pixels").
		self._effective_mode = "normalized"
		try:
			self._image_long_side = max(448, int(image_long_side))
		except (TypeError, ValueError):
			self._image_long_side = DEFAULT_IMAGE_LONG_SIDE
		# Per-session state (reset in prepare_interaction).
		self._no_action_count = 0
		self._display_size: tuple[int, int] = (DEFAULT_IMAGE_LONG_SIDE, 800)

	# -- Extra config --------------------------------------------------------

	@property
	def coordinate_mode(self) -> str:
		return self._coordinate_mode

	@coordinate_mode.setter
	def coordinate_mode(self, value: str) -> None:
		if value in COORDINATE_MODES:
			self._coordinate_mode = value

	@property
	def image_long_side(self) -> int:
		return self._image_long_side

	@property
	def refine_clicks(self) -> bool:
		return self._refine_clicks

	@refine_clicks.setter
	def refine_clicks(self, value: bool) -> None:
		self._refine_clicks = bool(value)

	# -- Serialisation -------------------------------------------------------

	def to_dict(self) -> dict:
		d = super().to_dict()
		d["coordinate_mode"] = self._coordinate_mode
		d["image_long_side"] = self._image_long_side
		d["refine_clicks"] = self._refine_clicks
		return d

	@classmethod
	def _from_dict(cls, data: dict) -> "OpenAICompatInteractionProvider":
		return cls(
			name=data.get("name", ""),
			api_url=data.get("api_url", ""),
			api_key=data.get("api_key", ""),
			model_fetch_url=data.get("model_fetch_url", ""),
			coordinate_mode=data.get("coordinate_mode", "auto"),
			image_long_side=data.get("image_long_side", DEFAULT_IMAGE_LONG_SIDE),
			refine_clicks=data.get("refine_clicks", True),
		)

	# -- Interaction (Computer Use) interface ----------------------------------

	@property
	def supports_interaction(self) -> bool:
		return True

	def prepare_interaction(
		self,
		*,
		user_request: str,
		screenshot_b64: str,
		screen_size: tuple[int, int] | None = None,
		model: str | None = None,
	) -> dict:
		self._no_action_count = 0
		display = compute_display_size(screen_size, long_side=self._image_long_side)
		self._display_size = display
		mode = resolve_coordinate_mode(self._coordinate_mode, model)
		self._effective_mode = mode
		if mode == "pixels":
			coordinate_size = display
		else:
			coordinate_size = (1000, 1000)
		log.info(
			"Interaction session: model=%r coordinate_mode=%s (configured %s) display=%s",
			model, mode, self._coordinate_mode, display,
		)

		messages: list[dict] = [
			{
				"role": "system",
				"content": build_system_prompt(
					coordinate_mode=mode,
					display_size=display,
				),
			},
			{
				"role": "user",
				"content": [
					{
						"type": "text",
						# Translators: not translated — this is sent to the model.
						"text": (
							f"Task: {user_request}\n\n"
							"Here is the current screenshot. Decide on the first action."
						),
					},
					_image_part(screenshot_b64),
				],
			},
		]
		return {
			"messages": messages,
			"tools": [],  # Prompted protocol — nothing to send as `tools`.
			"display_size": display,
			"coordinate_size": coordinate_size,
			# Hints for the agent loop.
			"refine_clicks": self._refine_clicks,
			"settle_delay": _SETTLE_DELAY,
			"image_format": _IMAGE_FORMAT,
			"image_quality": _IMAGE_QUALITY,
		}

	# -- Click refinement (two-stage grounding) ------------------------------

	@staticmethod
	def refine_crop_spec(screen_size: tuple[int, int]) -> dict:
		"""Return ``{"crop_size": (w, h), "zoom": n}`` for the refinement crop.

		The crop is in *screen* pixels and never larger than the screen.
		"""
		w, h = screen_size
		cw = min(_REFINE_CROP_SIZE[0], max(28, (w // 28) * 28))
		ch = min(_REFINE_CROP_SIZE[1], max(28, (h // 28) * 28))
		return {"crop_size": (cw, ch), "zoom": _REFINE_ZOOM}

	def refine_coordinate(
		self,
		*,
		model: str,
		crop_b64: str,
		crop_size: tuple[int, int],
		action: dict,
		timeout: int = 60,
	) -> tuple[int, int] | None:
		"""Second-stage grounding on a zoomed crop around the proposed point.

		Returns the refined ``(x, y)`` in *crop image pixels*, or ``None``
		when the model cannot locate the target (the caller then keeps
		the original coordinate).
		"""
		target = (action.get("target") or "").strip()
		if not target:
			return None
		cw, ch = crop_size
		mode = self._effective_mode
		if mode == "pixels":
			coord_hint = (
				f"The image is {cw}x{ch} pixels; give the coordinate in pixels of this image."
			)
		else:
			coord_hint = (
				"Give the coordinate on a 1000x1000 grid spanning this image "
				"((0,0) top-left, (1000,1000) bottom-right)."
			)
		prompt = (
			"This is an enlarged part of a Windows screen.\n"
			f"Locate this UI element: \"{target}\".\n"
			f"{coord_hint}\n"
			"Reply with only JSON: {\"found\": true, \"coordinate\": [x, y]} giving the "
			"exact centre of the element, or {\"found\": false} if it is not in this image."
		)
		payload = {
			"model": model,
			"messages": [
				{
					"role": "user",
					"content": [
						_image_part(crop_b64),
						{"type": "text", "text": prompt},
					],
				},
			],
			"temperature": 0.0,
			"max_tokens": 200,
		}
		data = self._http_json(
			self._chat_url(), headers=self._headers(),
			data=json.dumps(payload).encode("utf-8"), method="POST", timeout=timeout,
		)
		try:
			content = data["choices"][0]["message"].get("content") or ""
		except (KeyError, IndexError, TypeError):
			return None
		return parse_refinement(content, crop_size=crop_size, mode=mode)

	def send_interaction(
		self, *, model, messages, tools, max_tokens=4096, timeout=120,
	) -> dict:
		payload: dict = {
			"model": model,
			"messages": messages,
			"temperature": _AGENT_TEMPERATURE,
			"max_tokens": max_tokens,
		}
		data = self._post_with_shrink_retry(payload, messages, timeout=timeout)
		try:
			message = data["choices"][0]["message"]
		except (KeyError, IndexError, TypeError) as exc:
			log.error("Unexpected interaction response: %s", data)
			raise ValueError(
				"Unexpected response from the API. "
				"The model may not support this request format."
			) from exc

		parsed = parse_model_message(message)
		text = parsed["text"]
		action = parsed["action"]

		if action is None:
			self._no_action_count += 1
			done = self._no_action_count >= _MAX_NO_ACTION_RESPONSES
			if not done:
				log.info("No action in model reply — nudging once. Reply: %r", text[:200])
			return {
				"done": done,
				"text": [text] if text else [],
				"tool_calls": [],
				"raw_content": {"assistant_text": parsed["raw_text"], "nudge": not done},
			}

		self._no_action_count = 0

		if action["action"] in ("terminate", "answer"):
			final_parts = [text] if text else []
			extra = action.get("text") or ""
			status = action.get("status") or ""
			if extra and extra not in final_parts:
				final_parts.append(extra)
			if status and status != "success":
				final_parts.append(f"Status: {status}")
			return {
				"done": True,
				"text": final_parts,
				"tool_calls": [],
				"raw_content": {"assistant_text": parsed["raw_text"], "nudge": False},
			}

		return {
			"done": False,
			"text": [text] if text else [],
			"tool_calls": [
				{"id": "call_1", "name": "computer_use", "action": action},
			],
			"raw_content": {"assistant_text": parsed["raw_text"], "nudge": False},
		}

	def _post_with_shrink_retry(
		self, payload: dict, messages: list, *, timeout: int,
	) -> dict:
		"""POST *payload*; if the server rejects the body as too large,
		drop all but the newest screenshot from *messages* and retry once.

		*messages* is the same list object referenced by ``payload`` so the
		pruning is visible in the retried body and in the session history.
		"""
		try:
			return self._http_json(
				self._chat_url(), headers=self._headers(),
				data=json.dumps(payload).encode("utf-8"), method="POST",
				timeout=timeout,
			)
		except ConnectionError as exc:
			text = str(exc).lower()
			if not any(h in text for h in _TOO_LARGE_HINTS):
				raise
			n_images = _count_images(messages)
			if n_images <= 1:
				raise
			log.warning(
				"Request body rejected as too large (%s) — pruning history to "
				"1 screenshot (had %d) and retrying once.", exc, n_images,
			)
			prune_screenshot_history(messages, keep=1)
			return self._http_json(
				self._chat_url(), headers=self._headers(),
				data=json.dumps(payload).encode("utf-8"), method="POST",
				timeout=timeout,
			)

	def append_interaction_turn(
		self, messages, *, raw_content, tool_results,
	) -> None:
		assistant_text = ""
		nudge = False
		if isinstance(raw_content, dict):
			assistant_text = raw_content.get("assistant_text", "") or ""
			nudge = bool(raw_content.get("nudge"))
		messages.append({"role": "assistant", "content": assistant_text or "(no reply)"})

		if nudge or not tool_results:
			messages.append({
				"role": "user",
				"content": (
					"Your reply contained no action. Reply with one short sentence "
					"and then exactly one <tool_call>…</tool_call> block. "
					"Use action=terminate if the task is already complete."
				),
			})
			return

		parts: list[dict] = []
		for tr in tool_results:
			desc = describe_executed_action(tr.get("action"), tr.get("description"))
			parts.append({
				"type": "text",
				"text": (
					f"{desc}. Here is the screenshot after that action. "
					"Check the result carefully, then decide the next action "
					"or terminate if the task is complete."
				),
			})
			parts.append(_image_part(tr["screenshot_b64"]))
		messages.append({"role": "user", "content": parts})

		prune_screenshot_history(messages, keep=_MAX_SCREENSHOTS_IN_HISTORY)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_system_prompt(
	*, coordinate_mode: str, display_size: tuple[int, int],
) -> str:
	"""Return the agent system prompt for the given coordinate convention."""
	w, h = display_size
	if coordinate_mode == "pixels":
		resolution = f"{w}x{h}"
		coord_desc = (
			f"(x, y): The x (pixels from the left edge) and y (pixels from the top "
			f"edge) coordinates in the {w}x{h} screenshot."
		)
	else:
		# Mirrors the official Qwen computer-use recipe, which declares a
		# virtual 1000x1000 screen so the model emits normalised coordinates.
		resolution = "1000x1000"
		coord_desc = (
			"(x, y): The x (from the left edge) and y (from the top edge) "
			"coordinates on a 1000x1000 grid that spans the whole screenshot, "
			"regardless of its pixel size. (0,0) is the top-left corner and "
			"(1000,1000) the bottom-right corner."
		)

	tool_spec = {
		"type": "function",
		"function": {
			"name": "computer_use",
			"description": (
				"Use a mouse and keyboard to interact with a Windows computer, "
				"and take screenshots.\n"
				f"* The screen's resolution is {resolution}.\n"
				"* Whenever you intend to click on an element like an icon, button "
				"or text field, consult the screenshot to determine its coordinates "
				"and click in the centre of the element, not on its edge.\n"
				"* After each action you receive a new screenshot. Verify the "
				"action had the intended effect before continuing. If it did not, "
				"adjust the coordinates or try a different approach.\n"
				"* Prefer keyboard shortcuts when they are reliable (e.g. ctrl+s, "
				"alt+f4, tab, Return).\n"
				"* Some applications take time to react; use wait and take another "
				"look if the screen has not changed yet."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"action": {
						"type": "string",
						"enum": [
							"key", "type", "mouse_move", "left_click",
							"left_click_drag", "right_click", "middle_click",
							"double_click", "triple_click", "scroll", "hscroll",
							"wait", "terminate", "answer",
						],
						"description": (
							"The action to perform. The available actions are:\n"
							"* `key`: Press a key or key combination given in `keys`, "
							"e.g. [\"ctrl\", \"s\"] or [\"Return\"].\n"
							"* `type`: Type the string `text` on the keyboard.\n"
							"* `mouse_move`: Move the cursor to `coordinate`.\n"
							"* `left_click`: Left-click at `coordinate`.\n"
							"* `left_click_drag`: Drag from `start_coordinate` to `coordinate` "
							"with the left button held.\n"
							"* `right_click`: Right-click at `coordinate`.\n"
							"* `middle_click`: Middle-click at `coordinate`.\n"
							"* `double_click`: Double-click at `coordinate`.\n"
							"* `triple_click`: Triple-click at `coordinate`.\n"
							"* `scroll`: Scroll vertically at `coordinate` by `pixels` "
							"(positive = up, negative = down).\n"
							"* `hscroll`: Scroll horizontally at `coordinate` by `pixels` "
							"(positive = right, negative = left).\n"
							"* `wait`: Wait `time` seconds for the screen to change.\n"
							"* `terminate`: The task is finished (or impossible); report "
							"`status` and a short summary in `text`.\n"
							"* `answer`: Answer the user's question with `text` and stop."
						),
					},
					"keys": {
						"type": "array",
						"items": {"type": "string"},
						"description": "Required only by `action=key`.",
					},
					"text": {
						"type": "string",
						"description": "Required by `action=type`, `action=answer` and `action=terminate`.",
					},
					"coordinate": {
						"type": "array",
						"items": {"type": "number"},
						"description": coord_desc,
					},
					"target": {
						"type": "string",
						"description": (
							"Short description of the UI element at `coordinate`, e.g. "
							"\"Chromium icon in the taskbar\" or \"OK button\". Required "
							"by every action that has a `coordinate`."
						),
					},
					"start_coordinate": {
						"type": "array",
						"items": {"type": "number"},
						"description": "Drag start point. Required only by `action=left_click_drag`.",
					},
					"pixels": {
						"type": "number",
						"description": (
							"The amount of scrolling to perform. Positive values scroll "
							"up (or right), negative values scroll down (or left). "
							"Required only by `action=scroll` and `action=hscroll`."
						),
					},
					"time": {
						"type": "number",
						"description": "The seconds to wait. Required only by `action=wait`.",
					},
					"status": {
						"type": "string",
						"enum": ["success", "failure"],
						"description": "The status of the task. Required only by `action=terminate`.",
					},
				},
				"required": ["action"],
			},
		},
	}

	return (
		"You are Luma, an assistant that operates a Windows computer on behalf of "
		"a blind user who cannot see the screen. You see the screen through "
		"screenshots and act with a mouse and keyboard. Work step by step: "
		"exactly one action per reply.\n\n"
		"# Tools\n\n"
		"You may call one function to assist with the user query.\n\n"
		"You are provided with function signatures within <tools></tools> XML tags:\n"
		"<tools>\n"
		f"{json.dumps(tool_spec, ensure_ascii=False)}\n"
		"</tools>\n\n"
		"For each function call, return a json object with function name and "
		"arguments within <tool_call></tool_call> XML tags:\n"
		"<tool_call>\n"
		'{"name": <function-name>, "arguments": <args-json-object>}\n'
		"</tool_call>\n\n"
		"# Reply format\n\n"
		"Because the user cannot see the screen, start every reply with ONE short "
		"plain sentence (max 20 words) saying what you see and what you are about "
		"to do — it is read aloud. Then output exactly one <tool_call> block and "
		"nothing else. Never output more than one action per reply. Always fill "
		"`target` with a short name of the element you click on. When the task "
		"is complete, use action=terminate with a short summary in `text`."
	)


# ---------------------------------------------------------------------------
# Pure helpers (NVDA-free, unit-testable)
# ---------------------------------------------------------------------------

def resolve_coordinate_mode(configured: str, model: str | None) -> str:
	"""Return the effective coordinate mode (``"normalized"`` or ``"pixels"``).

	``"auto"`` inspects the model name: Qwen2.5-VL / UI-TARS style models
	emit absolute pixels, Qwen3-VL / Gemini style models a 0-1000 grid.
	Unknown names default to the normalised grid, which is the more
	common convention among recent models.
	"""
	if configured in ("normalized", "pixels"):
		return configured
	name = (model or "").lower()
	# Check pixel hints first: "qwen2.5-vl" must not be shadowed by anything.
	if any(h in name for h in _PIXEL_MODEL_HINTS):
		return "pixels"
	if any(h in name for h in _NORMALIZED_MODEL_HINTS):
		return "normalized"
	return "normalized"


def compute_display_size(
	screen_size: tuple[int, int] | None,
	*,
	long_side: int = DEFAULT_IMAGE_LONG_SIDE,
	factor: int = _SIZE_FACTOR,
) -> tuple[int, int]:
	"""Return the screenshot size to send: aspect-preserving, ≤ *long_side*.

	Both dimensions are rounded to multiples of *factor* (never below
	*factor*) so Qwen-style server-side ``smart_resize`` is an identity
	and pixel-mode coordinates line up exactly.
	"""
	if not screen_size or screen_size[0] <= 0 or screen_size[1] <= 0:
		screen_size = (1280, 800)
	w, h = screen_size
	scale = min(1.0, long_side / max(w, h))
	dw = max(factor, int(round(w * scale / factor)) * factor)
	dh = max(factor, int(round(h * scale / factor)) * factor)
	return dw, dh


def describe_executed_action(action: dict | None, fallback: str | None) -> str:
	"""Describe an executed action for the model, in *its* coordinate space.

	The executor's description reports real screen pixels, which would
	confuse a model that thinks in screenshot/grid coordinates.
	"""
	if not isinstance(action, dict):
		return fallback or "Action executed"
	name = action.get("action", "")
	coord = action.get("coordinate")
	target = action.get("target")
	where = ""
	if coord is not None:
		where = f" at ({coord[0]}, {coord[1]})"
		if target:
			where = f" on \"{target}\"{where}"
	if name in ("left_click", "right_click", "middle_click", "double_click",
				"triple_click", "mouse_move"):
		return f"Executed {name}{where}"
	if name == "type":
		return f"Typed the text {action.get('text', '')!r}"
	if name == "type_at":
		return f"Clicked{where} and typed {action.get('text', '')!r}"
	if name == "key":
		return f"Pressed key(s) {action.get('text', '')}"
	if name == "scroll":
		return (
			f"Scrolled {action.get('scroll_direction', 'down')} "
			f"{action.get('scroll_amount', 3)} step(s){where}"
		)
	if name == "scroll_page":
		return f"Scrolled the page {action.get('scroll_direction', 'down')}"
	if name == "left_click_drag":
		return f"Dragged to ({action['target_coordinate'][0]}, {action['target_coordinate'][1]})"
	if name == "wait":
		return f"Waited {action.get('duration', 1)} second(s)"
	return fallback or f"Executed {name}"


def parse_refinement(
	content: str, *, crop_size: tuple[int, int], mode: str,
) -> tuple[int, int] | None:
	"""Parse the refinement reply into crop-pixel ``(x, y)`` or ``None``."""
	content = _THINK_RE.sub("", content or "")
	obj = _first_json_object(content)
	if obj is None:
		# Fall back to the first two numbers in the reply.
		nums = _NUM_RE.findall(content)
		if len(nums) < 2:
			return None
		obj = {"coordinate": nums[:2]}
	if obj.get("found") is False:
		return None
	coord = _coord(
		obj.get("coordinate", obj.get("point", obj.get("center", obj.get("centre"))))
	)
	if coord is None and "bbox_2d" in obj and isinstance(obj["bbox_2d"], (list, tuple)) \
			and len(obj["bbox_2d"]) >= 4:
		b = obj["bbox_2d"]
		coord = _coord([(float(b[0]) + float(b[2])) / 2, (float(b[1]) + float(b[3])) / 2])
	if coord is None:
		return None
	cw, ch = crop_size
	x, y = coord
	if mode != "pixels":
		x = x / 1000 * cw
		y = y / 1000 * ch
	if not (0 <= x <= cw and 0 <= y <= ch):
		return None
	return int(round(x)), int(round(y))


def _image_part(screenshot_b64: str, mime: str = _IMAGE_MIME) -> dict:
	return {
		"type": "image_url",
		"image_url": {"url": f"data:{mime};base64,{screenshot_b64}"},
	}


def _count_images(messages: list) -> int:
	"""Number of image parts currently in *messages*."""
	n = 0
	for msg in messages:
		content = msg.get("content") if isinstance(msg, dict) else None
		if isinstance(content, list):
			n += sum(
				1 for p in content if isinstance(p, dict) and p.get("type") == "image_url"
			)
	return n


def prune_screenshot_history(messages: list[dict], *, keep: int) -> None:
	"""Replace all but the last *keep* screenshot images with a text placeholder.

	Mutates *messages* in place.  Only user messages with list content
	are inspected.
	"""
	seen = 0
	for msg in reversed(messages):
		if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
			continue
		new_parts: list[dict] = []
		for part in msg["content"]:
			if isinstance(part, dict) and part.get("type") == "image_url":
				seen += 1
				if seen > keep:
					new_parts.append({"type": "text", "text": _SCREENSHOT_PLACEHOLDER})
					continue
			new_parts.append(part)
		msg["content"] = new_parts


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def parse_model_message(message: dict) -> dict:
	"""Parse an OpenAI ``message`` object into ``{text, action, raw_text}``.

	* ``text`` — narration to speak (reply minus think/tool-call blocks).
	* ``action`` — normalised common action dict, or ``None``.
	* ``raw_text`` — the content as it should be echoed back into history.
	"""
	content = message.get("content") or ""
	if isinstance(content, list):
		# Some servers return content as parts.
		content = "".join(
			p.get("text", "") for p in content if isinstance(p, dict)
		)
	content = _THINK_RE.sub("", content).strip()

	action_obj: dict | None = None

	# 1. Server-side parsed tool calls (vLLM --enable-auto-tool-choice …).
	for tc in message.get("tool_calls") or []:
		fn = tc.get("function") or {}
		args = fn.get("arguments")
		if isinstance(args, str):
			try:
				args = json.loads(args)
			except json.JSONDecodeError:
				args = None
		if isinstance(args, dict):
			action_obj = args
			break

	text, inline = extract_action_json(content)
	if action_obj is None:
		action_obj = inline

	action = normalise_action(action_obj) if action_obj is not None else None

	raw_text = content
	if not raw_text and action_obj is not None:
		# Keep history consistent even when the server stripped the call.
		raw_text = (
			"<tool_call>\n"
			+ json.dumps({"name": "computer_use", "arguments": action_obj})
			+ "\n</tool_call>"
		)
	return {"text": text, "action": action, "raw_text": raw_text}


def extract_action_json(content: str) -> tuple[str, dict | None]:
	"""Split *content* into ``(narration_text, action_json_or_None)``.

	Accepts ``<tool_call>`` wrappers, fenced ```json blocks, and bare
	JSON objects.  ``{"name": ..., "arguments": {...}}`` envelopes are
	unwrapped.
	"""
	if not content:
		return "", None

	m = _TOOL_CALL_RE.search(content)
	if m:
		obj = _loads_lenient(m.group(1))
		text = (content[: m.start()] + content[m.end():]).strip()
		return _clean_text(text), _unwrap(obj)

	m = _FENCE_RE.search(content)
	if m:
		obj = _loads_lenient(m.group(1))
		if obj is not None:
			text = (content[: m.start()] + content[m.end():]).strip()
			return _clean_text(text), _unwrap(obj)

	# Bare JSON object somewhere in the text — find balanced braces.
	start = content.find("{")
	while start != -1:
		end = _match_brace(content, start)
		if end != -1:
			obj = _loads_lenient(content[start:end + 1])
			if isinstance(obj, dict) and (
				"action" in obj or "arguments" in obj or "name" in obj
			):
				text = (content[:start] + content[end + 1:]).strip()
				return _clean_text(text), _unwrap(obj)
		start = content.find("{", start + 1)

	return _clean_text(content), None


def _first_json_object(content: str) -> dict | None:
	"""Return the first JSON object in *content* (any keys), or ``None``.

	Looks inside ``<tool_call>`` and fenced blocks first, then scans for
	the first balanced ``{…}``.  A top-level JSON *array* of objects (the
	Qwen grounding format) yields its first element.
	"""
	if not content:
		return None
	for rx in (_TOOL_CALL_RE, _FENCE_RE):
		m = rx.search(content)
		if m:
			obj = _loads_lenient(m.group(1))
			if isinstance(obj, list) and obj and isinstance(obj[0], dict):
				return obj[0]
			if isinstance(obj, dict):
				return _unwrap(obj)
	start = content.find("{")
	while start != -1:
		end = _match_brace(content, start)
		if end != -1:
			obj = _loads_lenient(content[start:end + 1])
			if isinstance(obj, dict):
				return _unwrap(obj)
		start = content.find("{", start + 1)
	return None


def _clean_text(text: str) -> str:
	# Drop stray wrapper tags / fences the model may leave behind.
	text = re.sub(r"</?tool_call>", "", text, flags=re.IGNORECASE)
	text = text.replace("```json", "").replace("```", "")
	return " ".join(text.split())


def _match_brace(s: str, start: int) -> int:
	depth = 0
	in_str = False
	esc = False
	for i in range(start, len(s)):
		ch = s[i]
		if in_str:
			if esc:
				esc = False
			elif ch == "\\":
				esc = True
			elif ch == '"':
				in_str = False
			continue
		if ch == '"':
			in_str = True
		elif ch == "{":
			depth += 1
		elif ch == "}":
			depth -= 1
			if depth == 0:
				return i
	return -1


def _loads_lenient(s: str):
	s = s.strip()
	try:
		return json.loads(s)
	except json.JSONDecodeError:
		pass
	# Common model slips: trailing commas, single quotes.
	fixed = re.sub(r",\s*([}\]])", r"\1", s)
	try:
		return json.loads(fixed)
	except json.JSONDecodeError:
		pass
	try:
		return json.loads(fixed.replace("'", '"'))
	except json.JSONDecodeError:
		return None


def _unwrap(obj) -> dict | None:
	if not isinstance(obj, dict):
		return None
	if isinstance(obj.get("arguments"), dict):
		return obj["arguments"]
	if isinstance(obj.get("arguments"), str):
		inner = _loads_lenient(obj["arguments"])
		return inner if isinstance(inner, dict) else None
	if isinstance(obj.get("parameters"), dict):
		return obj["parameters"]
	return obj


# Synonyms → canonical action names used by interaction.actions.
_ACTION_ALIASES: dict[str, str] = {
	"click": "left_click", "left_click": "left_click", "tap": "left_click",
	"single_click": "left_click", "mouse_click": "left_click",
	"right_click": "right_click", "middle_click": "middle_click",
	"double_click": "double_click", "doubleclick": "double_click",
	"triple_click": "triple_click",
	"mouse_move": "mouse_move", "hover": "mouse_move", "move": "mouse_move",
	"move_to": "mouse_move", "hover_at": "mouse_move",
	"type": "type", "type_text": "type", "input": "type", "input_text": "type",
	"write": "type", "type_at": "type_at", "type_text_at": "type_at",
	"key": "key", "hotkey": "key", "press": "key", "keypress": "key",
	"press_key": "key", "key_combination": "key", "keyboard": "key",
	"scroll": "scroll", "hscroll": "hscroll", "scroll_at": "scroll",
	"scroll_page": "scroll_page", "scroll_document": "scroll_page",
	"left_click_drag": "left_click_drag", "drag": "left_click_drag",
	"drag_and_drop": "left_click_drag", "drag_to": "left_click_drag",
	"wait": "wait", "sleep": "wait",
	"screenshot": "screenshot", "take_screenshot": "screenshot",
	"terminate": "terminate", "finish": "terminate", "finished": "terminate",
	"done": "terminate", "complete": "terminate", "stop": "terminate",
	"end": "terminate", "answer": "answer", "call_user": "answer",
	"respond": "answer", "reply": "answer",
}

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _coord(value) -> list[int] | None:
	"""Normalise a coordinate given as list, dict, or string to ``[x, y]``."""
	if value is None:
		return None
	if isinstance(value, dict):
		x, y = value.get("x"), value.get("y")
		if x is None or y is None:
			return None
		value = [x, y]
	if isinstance(value, str):
		nums = _NUM_RE.findall(value)
		if len(nums) < 2:
			return None
		value = nums[:2]
	if isinstance(value, (list, tuple)) and len(value) >= 2:
		try:
			return [int(round(float(value[0]))), int(round(float(value[1])))]
		except (TypeError, ValueError):
			return None
	return None


def normalise_action(args: dict) -> dict | None:
	"""Convert a model-emitted argument dict to Luma's common action format.

	Returns ``None`` when no recognisable action is present.  Coordinates
	are left in the model's coordinate space; :mod:`interaction.scaling`
	maps them to the screen using ``coordinate_size``.
	"""
	if not isinstance(args, dict):
		return None
	raw_name = str(args.get("action") or args.get("type") or args.get("name") or "").strip()
	name = _ACTION_ALIASES.get(raw_name.lower().replace(" ", "_").replace("-", "_"))
	if not name:
		log.warning("Unknown model action: %r (%r)", raw_name, args)
		return None

	coord = _coord(
		args.get("coordinate", args.get("coordinates", args.get("position", args.get("point"))))
	)
	if coord is None and ("x" in args and "y" in args):
		coord = _coord({"x": args["x"], "y": args["y"]})

	target = args.get("target", args.get("element", args.get("description")))
	target = str(target).strip() if isinstance(target, str) and target.strip() else None

	def _with_target(d: dict) -> dict:
		if target and d.get("coordinate") is not None:
			d["target"] = target
		return d

	if name in ("left_click", "right_click", "middle_click", "double_click",
				"triple_click", "mouse_move"):
		if coord is None:
			return None
		return _with_target({"action": name, "coordinate": coord})

	if name == "type":
		text = args.get("text", args.get("content", args.get("value", "")))
		if coord is not None:
			return _with_target({"action": "type_at", "coordinate": coord, "text": str(text)})
		return {"action": "type", "text": str(text)}

	if name == "type_at":
		text = args.get("text", args.get("content", ""))
		if coord is None:
			return {"action": "type", "text": str(text)}
		return _with_target({
			"action": "type_at", "coordinate": coord, "text": str(text),
			"clear_before_typing": bool(args.get("clear_before_typing", False)),
			"press_enter": bool(args.get("press_enter", False)),
		})

	if name == "key":
		keys = args.get("keys", args.get("key", args.get("text", args.get("combo", ""))))
		if isinstance(keys, (list, tuple)):
			combo = "+".join(str(k).strip() for k in keys if str(k).strip())
		else:
			combo = str(keys).strip()
			# "ctrl c" / "ctrl + c" → "ctrl+c"
			combo = re.sub(r"\s*\+\s*", "+", combo)
			if "+" not in combo and " " in combo:
				combo = "+".join(combo.split())
		if not combo:
			return None
		return {"action": "key", "text": combo}

	if name in ("scroll", "hscroll"):
		horizontal = name == "hscroll"
		direction = str(args.get("scroll_direction", args.get("direction", ""))).lower()
		pixels = args.get("pixels", args.get("amount", args.get("magnitude")))
		amount = args.get("scroll_amount")
		if direction not in ("up", "down", "left", "right"):
			try:
				p = float(pixels) if pixels is not None else -300.0
			except (TypeError, ValueError):
				p = -300.0
			if horizontal:
				direction = "right" if p > 0 else "left"
			else:
				direction = "up" if p > 0 else "down"
			if amount is None:
				amount = max(1, min(10, int(round(abs(p) / 100)) or 1))
		if amount is None:
			amount = 3
		try:
			amount = max(1, int(amount))
		except (TypeError, ValueError):
			amount = 3
		if coord is None:
			return {"action": "scroll_page", "scroll_direction": direction}
		return _with_target({
			"action": "scroll", "coordinate": coord,
			"scroll_direction": direction, "scroll_amount": amount,
		})

	if name == "scroll_page":
		direction = str(args.get("scroll_direction", args.get("direction", "down"))).lower()
		if direction not in ("up", "down", "left", "right"):
			direction = "down"
		return {"action": "scroll_page", "scroll_direction": direction}

	if name == "left_click_drag":
		start = _coord(args.get("start_coordinate", args.get("from", args.get("start"))))
		end = _coord(
			args.get("target_coordinate", args.get("destination", args.get("to", args.get("end"))))
		)
		if end is None:
			end = coord          # Qwen style: `coordinate` is the drop target.
		elif start is None:
			start = coord        # Gemini/Anthropic-ish: `coordinate` is the start.
		if end is None:
			return None
		result = {"action": "left_click_drag", "target_coordinate": end}
		if start is not None:
			result["coordinate"] = start
		return result

	if name == "wait":
		duration = args.get("time", args.get("duration", args.get("seconds", 1)))
		try:
			duration = max(1, min(10, int(round(float(duration)))))
		except (TypeError, ValueError):
			duration = 1
		return {"action": "wait", "duration": duration}

	if name == "screenshot":
		return {"action": "screenshot"}

	if name in ("terminate", "answer"):
		return {
			"action": name,
			"status": str(args.get("status", "")),
			"text": str(args.get("text", args.get("message", args.get("answer", "")))),
		}

	return None
