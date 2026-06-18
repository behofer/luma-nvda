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
from .scaling import resize_screenshot


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
		config = self._provider.prepare_interaction(
			user_request="", screenshot_b64="",
		)
		display_size = config["display_size"]
		coordinate_size = config["coordinate_size"]

		# Capture and resize initial screenshot.
		wx.CallAfter(self._on_step, _("Taking screenshot..."))
		screenshot = capture_screen()
		if screenshot is None:
			wx.CallAfter(self._on_error, _("Failed to capture screen."))
			return
		screenshot = resize_screenshot(screenshot, display_size=display_size)

		# Build the real initial state with the resized screenshot.
		setup = self._provider.prepare_interaction(
			user_request=self._user_request,
			screenshot_b64=screenshot,
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

				description = actions.execute_action(
					action, self._screen_region,
					coordinate_size=coordinate_size,
				)
				# Translators: Spoken when an action is executed.
				wx.CallAfter(
					self._on_step,
					_("Step {step}: {desc}").format(
						step=step, desc=description,
					),
				)

				# Brief pause after action for the UI to settle.
				if action_name not in ("screenshot", "wait"):
					time.sleep(0.3)

				# Capture fresh screenshot for the tool result.
				new_screenshot = capture_screen()
				if new_screenshot is None:
					log.warning("Screenshot capture failed after action.")
					continue

				tool_results.append({
					"id": tc["id"],
					"name": tc.get("name", ""),
					"screenshot_b64": resize_screenshot(
						new_screenshot, display_size=display_size,
					),
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
