"""Agentic loop for Computer Use (provider-agnostic).

Implements the screenshot-API-execute cycle:

1. Capture screen, resize to the provider's display dimensions.
2. Call :meth:`provider.send_interaction` with messages + tools.
3. Parse the normalised response — execute actions, report text.
4. Append assistant turn + tool results (fresh screenshots) via
   :meth:`provider.append_interaction_turn`.
5. Repeat until ``done`` or step limit reached.

The loop runs in a background thread via :func:`worker.run_in_background`.
Status updates are dispatched to the main thread through callbacks.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import wx

log = logging.getLogger(__name__)

from . import actions
from .scaling import crop_and_zoom, resize_screenshot, scale_to_screen

# Actions that carry a `coordinate` the provider may want to refine.
_REFINABLE_ACTIONS = frozenset({
	"left_click", "right_click", "middle_click", "double_click",
	"triple_click", "mouse_move", "type_at",
})


class InteractionLoop:
	"""Encapsulates the Computer Use agentic loop.

	Parameters
	----------
	provider:
		A provider instance whose ``supports_interaction`` is ``True``.
	model:
		Model ID (e.g. ``"claude-sonnet-4-20250514"``).
	user_request:
		The user's natural-language task description.
	screen_region:
		``(left, top, width, height)`` of the screen area to control.
	on_step:
		Callback ``(text) -> None`` invoked on the **main thread** for
		each progress update (reasoning, action descriptions).
	on_complete:
		Callback ``(text) -> None`` invoked on the **main thread** when
		the loop finishes (final text or limit message).
	on_error:
		Callback ``(text) -> None`` invoked on the **main thread** when
		an unrecoverable error occurs.
	cancel_event:
		Set this event to gracefully stop the loop after the current
		iteration.
	max_steps:
		Safety limit on the number of loop iterations.
	"""

	def __init__(
		self,
		provider,
		*,
		model: str,
		user_request: str,
		screen_region: tuple[int, int, int, int],
		on_step: Callable[[str], None],
		on_complete: Callable[[str], None],
		on_error: Callable[[str], None],
		cancel_event: threading.Event,
		max_steps: int = 50,
	) -> None:
		self._provider = provider
		self._model = model
		self._user_request = user_request
		self._screen_region = screen_region
		self._on_step = on_step
		self._on_complete = on_complete
		self._on_error = on_error
		self._cancel = cancel_event
		self._max_steps = max_steps

	# -- Public entry point (called in background thread) ------------------

	def run(self) -> None:
		"""Execute the agentic loop.  Blocks until complete or cancelled."""
		try:
			self._run_loop()
		except Exception as exc:
			log.exception("Interaction loop failed")
			wx.CallAfter(self._on_error, str(exc))

	def _run_loop(self) -> None:
		from ..capture import capture_screen

		# Discover the provider's display and coordinate sizes
		# (lightweight — no API call, just builds config).
		screen_size = (self._screen_region[2], self._screen_region[3])
		config = self._provider.prepare_interaction(
			user_request="", screenshot_b64="", screen_size=screen_size,
			model=self._model,
		)
		display_size = config["display_size"]
		coordinate_size = config["coordinate_size"]
		# Optional provider hints (prompted providers use these).
		refine_clicks = bool(config.get("refine_clicks")) and hasattr(
			self._provider, "refine_coordinate",
		)
		settle_delay = float(config.get("settle_delay", 0.3))
		# Encoding of screenshots sent to the model.  Native providers get
		# PNG (their request formats say so); prompted providers may ask
		# for JPEG to keep request bodies small.
		image_format = config.get("image_format", "png")
		image_quality = int(config.get("image_quality", 85))

		def _for_model(b64_png: str) -> str:
			return resize_screenshot(
				b64_png, display_size=display_size,
				fmt=image_format, quality=image_quality,
			)

		# Capture and resize initial screenshot.
		wx.CallAfter(self._on_step, _("Taking screenshot..."))
		full_screenshot = capture_screen()
		if full_screenshot is None:
			wx.CallAfter(self._on_error, _("Failed to capture screen."))
			return
		screenshot = _for_model(full_screenshot)

		# Build the real initial state with the resized screenshot.
		setup = self._provider.prepare_interaction(
			user_request=self._user_request,
			screenshot_b64=screenshot,
			screen_size=screen_size,
			model=self._model,
		)
		messages = setup["messages"]
		tools = setup["tools"]

		for step in range(1, self._max_steps + 1):
			if self._cancel.is_set():
				wx.CallAfter(self._on_complete, _("Cancelled by user."))
				return

			# Translators: Status shown during interaction loop.
			wx.CallAfter(
				self._on_step,
				_("Step {step}/{max}: Sending to AI...").format(
					step=step, max=self._max_steps,
				),
			)

			response = self._provider.send_interaction(
				model=self._model,
				messages=messages,
				tools=tools,
			)

			# Report reasoning text.
			for text in response["text"]:
				wx.CallAfter(self._on_step, text)

			# If the model is done, finish.
			if response["done"]:
				final = (
					"\n".join(response["text"])
					if response["text"]
					else _("Task completed.")
				)
				wx.CallAfter(self._on_complete, final)
				return

			# Execute each action and collect tool results.
			tool_results: list[dict] = []
			for tc in response["tool_calls"]:
				action = tc["action"]
				action_name = action.get("action", "")

				if (
					refine_clicks
					and action_name in _REFINABLE_ACTIONS
					and action.get("coordinate") is not None
				):
					action = self._refine_action(
						action, full_screenshot,
						coordinate_size=coordinate_size,
						image_format=image_format,
					)

				# Tell the user exactly what is about to happen (element,
				# position, text, keys) before doing it.
				spoken = actions.describe_action(
					action, self._screen_region,
					coordinate_size=coordinate_size,
				)
				# Translators: Spoken right before an action is executed.
				wx.CallAfter(
					self._on_step,
					_("Step {step}: {desc}").format(step=step, desc=spoken),
				)

				description = actions.execute_action(
					action, self._screen_region,
					coordinate_size=coordinate_size,
				)
				if description.startswith(("Failed", "Unknown")):
					# Translators: Spoken when an action could not be executed.
					wx.CallAfter(
						self._on_step,
						_("Action failed: {desc}").format(desc=description),
					)

				# Pause after the action so the UI can settle before the
				# next screenshot — otherwise the model acts on stale state.
				if action_name not in ("screenshot", "wait"):
					time.sleep(settle_delay)

				# Capture fresh screenshot for the tool result.
				new_screenshot = capture_screen()
				if new_screenshot is None:
					log.warning("Screenshot capture failed after action.")
					continue
				full_screenshot = new_screenshot

				tool_results.append({
					"id": tc["id"],
					"name": tc.get("name", ""),
					# Human-readable result of the action and the action
					# itself; prompted providers echo them back to the
					# model, native ones ignore them.
					"description": description,
					"action": action,
					"screenshot_b64": _for_model(new_screenshot),
				})

			# Append the turn to the message history.
			self._provider.append_interaction_turn(
				messages,
				raw_content=response["raw_content"],
				tool_results=tool_results,
			)

		# Reached max steps.
		# Translators: Shown when the interaction loop hits its step limit.
		wx.CallAfter(
			self._on_complete,
			_("Maximum steps ({max}) reached. The task may not be complete.").format(
				max=self._max_steps,
			),
		)

	# -- Click refinement (two-stage grounding) -----------------------------

	def _refine_action(
		self,
		action: dict,
		full_screenshot_b64: str,
		*,
		coordinate_size: tuple[int, int],
		image_format: str = "png",
	) -> dict:
		"""Ask the provider for a more precise coordinate using a zoomed crop.

		The region around the proposed click point is cut from the
		*full-resolution* screenshot, enlarged, and handed to
		``provider.refine_coordinate``.  On any failure the original
		action is returned unchanged.
		"""
		try:
			left, top, width, height = self._screen_region
			cx, cy = scale_to_screen(
				int(action["coordinate"][0]), int(action["coordinate"][1]),
				self._screen_region, coordinate_size=coordinate_size,
			)
			spec = self._provider.refine_crop_spec((width, height))
			crop_w, crop_h, zoom = spec["crop_size"][0], spec["crop_size"][1], spec["zoom"]
			crop_left = min(max(cx - left - crop_w // 2, 0), max(0, width - crop_w))
			crop_top = min(max(cy - top - crop_h // 2, 0), max(0, height - crop_h))
			crop_b64 = crop_and_zoom(
				full_screenshot_b64, (crop_left, crop_top, crop_w, crop_h),
				zoom=zoom, fmt=image_format, quality=90,
			)
			if crop_b64 is None:
				return action

			point = self._provider.refine_coordinate(
				model=self._model,
				crop_b64=crop_b64,
				crop_size=(crop_w * zoom, crop_h * zoom),
				action=action,
			)
			if point is None:
				return action
			px, py = point
			# Zoomed-crop pixels → screen pixels → provider coordinate space.
			sx = left + crop_left + px / zoom
			sy = top + crop_top + py / zoom
			api_x = int(round((sx - left) / width * coordinate_size[0]))
			api_y = int(round((sy - top) / height * coordinate_size[1]))
			log.info(
				"Refined %s: (%s) -> (%s, %s) [screen %d,%d]",
				action.get("action"), action["coordinate"], api_x, api_y, int(sx), int(sy),
			)
			refined = dict(action)
			refined["coordinate"] = [api_x, api_y]
			return refined
		except Exception:
			log.exception("Click refinement failed — using original coordinate")
			return action
