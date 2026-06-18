"""Live mode controller — proactive continuous screen narration.

Live mode captures the screen every few seconds and sends each frame
to the configured AI provider using the builtin "Live" skill, which
instructs the model to emit very short, change-only narration. Prior
exchanges are kept in a rolling history so the model can refer back
to what it has already described and avoid repeating itself.

Design notes
------------
* All wx work happens on the main thread via ``wx.CallLater`` /
  ``wx.CallAfter``. Capture and the API call are dispatched to a
  background thread through :func:`worker.run_in_background`.
* Only one request is in flight at a time. If a tick fires while a
  previous request is still pending, that tick is skipped — piling up
  requests would cause the narration to lag further and further behind
  the screen.
* The processing sound is suppressed (``silent=True``). Live mode is
  loud enough on its own; a ticking click on every frame would be
  exhausting.
* The rolling history keeps the last ``HISTORY_TURNS`` assistant
  responses, which are replayed to the model as a single system
  "recent frames" message per tick. Putting them in a system message
  avoids the OpenAI-compat / Anthropic quirk where ``image_base64``
  would otherwise attach to the *oldest* user message when there are
  multiple user turns in the history.
* The model is instructed to reply with exactly ``"."`` when nothing
  has changed since the previous frame. That response is appended to
  history (so the model keeps its temporal bearings) but not spoken.

This module is a POC. It intentionally has no user-interaction UI —
the only entry and exit points are the ``NVDA+Shift+K`` toggle and
the automatic shutdown in ``GlobalPlugin.terminate``.
"""

from __future__ import annotations

import collections
import logging
import threading
from typing import Deque

import wx

import tones
import ui

log = logging.getLogger(__name__)


# How often we capture and send a new frame (milliseconds). 2 seconds
# is the sweet spot observed in the user's reference apps (Scribe Me,
# Orion) — short enough to feel live, long enough to stay under the
# round-trip time of most cloud vision models.
TICK_INTERVAL_MS: int = 2000

# How many previous assistant responses to replay as context. Matches
# the number the user asked for in the POC spec.
HISTORY_TURNS: int = 10

# Per-request timeout. Deliberately short — if the model can't answer
# within this window, the frame is stale anyway and we'd rather drop
# it than block the next tick.
REQUEST_TIMEOUT_S: int = 20

# Start / stop beep tones (frequency Hz, duration ms). Using NVDA's
# ``tones.beep`` keeps this cheap, localisation-free, and consistent
# with the rest of the screenreader's auditory feedback.
_START_BEEP = (880, 120)
_STOP_BEEP = (440, 120)

# Sentinel emitted by the Live skill when nothing has changed since the
# previous frame. We suppress speech for this response but still store
# it in history so the model knows what it just replied.
_NO_CHANGE_SENTINEL = "."


class LiveMode:
	"""Controller for a single live-narration session.

	Lifecycle: ``start()`` schedules the first tick and plays the start
	beep. Each tick captures the screen on the main thread, hands the
	bytes to a background worker for the API call, and on success
	appends the response to history and speaks it. ``stop()`` cancels
	the upcoming tick, marks in-flight requests as cancelled, and plays
	the stop beep. The instance is reusable — ``start()`` after
	``stop()`` begins a fresh session with empty history.
	"""

	def __init__(self, plugin) -> None:
		self._plugin = plugin
		self._running: bool = False
		self._timer: wx.CallLater | None = None
		# Rolling history of assistant responses. Deque with maxlen
		# auto-evicts the oldest entry once we exceed ``HISTORY_TURNS``.
		self._history: Deque[str] = collections.deque(maxlen=HISTORY_TURNS)
		# Set when a tick's request is in flight. Prevents overlapping
		# requests (see module docstring).
		self._in_flight: bool = False
		# Per-session cancel event — set by ``stop()`` so background
		# callbacks from an in-flight request become no-ops.
		self._cancel: threading.Event | None = None

	# ------------------------------------------------------------------
	# Public control
	# ------------------------------------------------------------------

	@property
	def is_running(self) -> bool:
		return self._running

	def start(self) -> None:
		"""Begin live narration. No-op if already running."""
		if self._running:
			return
		# Resolve provider + skill before the first beep so any config
		# error surfaces immediately and the user isn't left wondering
		# why nothing happens after the start beep.
		skill = self._find_live_skill()
		if skill is None:
			# Translators: Spoken when the Live skill is missing.
			ui.message(_("Live skill not found."))
			return
		provider, model = self._plugin._resolve_provider_and_model(skill)
		if provider is None:
			return

		self._skill = skill
		self._provider = provider
		self._model = model
		self._temperature = self._plugin._resolve_temperature(skill)
		self._history.clear()
		self._cancel = threading.Event()
		self._in_flight = False
		self._running = True

		_play_beep(_START_BEEP)
		# Translators: Spoken when Live mode starts.
		ui.message(_("Live mode on."))
		# Fire the first tick immediately — users expect prompt
		# feedback after pressing the toggle, not a 2-second wait.
		wx.CallLater(0, self._tick)

	def stop(self) -> None:
		"""End live narration. No-op if not running."""
		if not self._running:
			return
		self._running = False
		if self._cancel is not None:
			self._cancel.set()
		if self._timer is not None:
			try:
				self._timer.Stop()
			except Exception:
				log.debug("Live timer stop failed", exc_info=True)
			self._timer = None
		_play_beep(_STOP_BEEP)
		# Translators: Spoken when Live mode stops.
		ui.message(_("Live mode off."))

	def toggle(self) -> None:
		"""Start if stopped, stop if running. Bound to NVDA+Shift+K."""
		if self._running:
			self.stop()
		else:
			self.start()

	# ------------------------------------------------------------------
	# Tick loop
	# ------------------------------------------------------------------

	def _schedule_next(self) -> None:
		"""Schedule the next tick, unless we've been stopped in the meantime."""
		if not self._running:
			return
		self._timer = wx.CallLater(TICK_INTERVAL_MS, self._tick)

	def _tick(self) -> None:
		"""One live-narration cycle: capture → send → speak → reschedule.

		Runs on the main thread. The API call is dispatched to a
		background worker; the reschedule happens in the worker's
		main-thread callbacks so we never pile up timers.
		"""
		if not self._running:
			return

		# Skip if a previous request hasn't completed yet. The next
		# tick will pick up a fresher frame anyway.
		if self._in_flight:
			self._schedule_next()
			return

		from .. import capture

		image = capture.capture_screen()
		if image is None:
			# Transient capture failure — log and try again next tick.
			log.warning("Live: screen capture failed; skipping tick")
			self._schedule_next()
			return

		messages = self._build_messages()
		self._in_flight = True
		cancel = self._cancel

		def _call():
			# Image optimisation keeps payloads small; the network
			# round-trip is the dominant cost of each tick.
			optimised = capture.optimize_image(image)
			return self._provider.process_request(
				model=self._model,
				messages=messages,
				temperature=self._temperature,
				image_base64=optimised,
				timeout=REQUEST_TIMEOUT_S,
			)

		def _on_success(text: str) -> None:
			self._in_flight = False
			# Session was stopped while the request was in flight —
			# drop the response silently.
			if not self._running or (cancel is not None and cancel.is_set()):
				return
			self._handle_response(text)
			self._schedule_next()

		def _on_error(exc: Exception) -> None:
			self._in_flight = False
			if not self._running or (cancel is not None and cancel.is_set()):
				return
			# Swallow errors silently so a single transient failure
			# doesn't machine-gun the user with error beeps. Log only.
			log.warning("Live tick failed: %s", exc)
			self._schedule_next()

		from ..worker import run_in_background

		run_in_background(
			_call,
			on_success=_on_success,
			on_error=_on_error,
			silent=True,
			cancel_event=cancel,
		)

	# ------------------------------------------------------------------
	# Messages / history
	# ------------------------------------------------------------------

	def _build_messages(self) -> list[dict]:
		"""Construct the message list for one live tick.

		Layout:

		1. System message: the rendered Live skill prompt, with the
		   ``{Language}`` placeholder filled from NVDA's current UI
		   language so responses always match what the screenreader
		   is speaking.
		2. System message (optional): the rolling list of prior
		   responses, so the model has temporal context without the
		   image-attachment ambiguity of a multi-user-turn history.
		3. User message: the per-frame request. The current frame's
		   image is attached by the provider via the ``image_base64``
		   parameter.
		"""
		# NVDA language wins; fall back to the skill's explicit language
		# only if it's set to something other than the literal "Default".
		language = _nvda_language()
		if not language:
			language = self._skill.language if self._skill.language != "Default" else "en"
		messages: list[dict] = [
			{"role": "system", "content": self._skill.render_prompt(Language=language)},
		]
		if self._history:
			lines = [
				# Translators: Header for the rolling context of prior
				# Live-mode responses passed to the AI on each frame.
				_("Your recent responses (oldest first):"),
			]
			for idx, entry in enumerate(self._history, start=1):
				lines.append(f"{idx}. {entry}")
			messages.append({"role": "system", "content": "\n".join(lines)})
		messages.append({
			"role": "user",
			# Translators: Per-frame instruction sent to the AI in Live mode.
			"content": _("Narrate the current frame following the Live guidelines."),
		})
		return messages

	def _handle_response(self, text: str) -> None:
		"""Speak *text* (unless it's the no-change sentinel) and record it."""
		cleaned = (text or "").strip()
		if not cleaned:
			return
		# Always record the assistant's reply — even "." — so the
		# model knows the sequence of its own answers.
		self._history.append(cleaned)
		if cleaned == _NO_CHANGE_SENTINEL:
			return
		ui.message(cleaned)

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------

	@staticmethod
	def _find_live_skill():
		"""Locate the "Live" builtin skill, or return ``None``."""
		from ..skill import load_skills

		for sk in load_skills():
			if sk.name.lower() == "live":
				return sk
		return None


def _play_beep(spec: tuple[int, int]) -> None:
	"""Play a short NVDA ``tones.beep``. Swallows any errors — the
	beep is a nice-to-have, not worth breaking start/stop over.
	"""
	freq, duration = spec
	try:
		tones.beep(freq, duration)
	except Exception:
		log.debug("Live beep failed", exc_info=True)


def _nvda_language() -> str:
	"""Return NVDA's current UI language as a primary ISO-639-1 subtag
	(e.g. ``"de"`` for ``"de_DE"``), or an empty string if it cannot
	be determined. LLMs understand bare language codes directly, so no
	name-mapping is needed.
	"""
	try:
		import languageHandler
		nvda_lang = languageHandler.getLanguage()
		if nvda_lang:
			return nvda_lang.split("_")[0]
	except Exception:
		log.debug("Could not determine NVDA language", exc_info=True)
	return ""
