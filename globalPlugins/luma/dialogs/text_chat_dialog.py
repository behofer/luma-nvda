"""Text chat dialog for Luma.

Provides a conversational chat interface with skill selection,
conversation history management, and reasoning tag stripping.
"""

from __future__ import annotations

import logging
import re
import threading

import wx

import ui

from .. import config as luma_config
from ..providers import provider_from_dict
from ..skill import Skill, load_skills
from ..worker import run_in_background

log = logging.getLogger(__name__)

# Regex to strip reasoning/thinking tags from model responses.
_REASONING_RE = re.compile(
	r"<(?:think|thinking|reasoning|thought)>.*?</(?:think|thinking|reasoning|thought)>",
	re.DOTALL,
)

# Maximum number of non-system messages to keep in full before truncating.
_TRUNCATION_THRESHOLD = 10


def _strip_reasoning(text: str) -> str:
	"""Remove reasoning/thinking XML blocks from *text*."""
	return _REASONING_RE.sub("", text).strip()


class TextChatDialog(wx.Dialog):
	"""A conversational chat interface for Luma.

	Maintains a message history and sends requests through a provider.
	Supports switching skills mid-conversation via a dropdown.
	"""

	def __init__(
		self,
		parent: wx.Window | None = None,
		provider=None,
		model: str = "",
		skill: Skill | None = None,
		image_base64: str | None = None,
		temperature: float = 0.3,
		clipboard_text: str | None = None,
		initial_exchange: tuple[str, str] | None = None,
	) -> None:
		# Translators: Title of the text chat dialog.
		title = _("Luma Chat")
		if skill:
			title = _("Luma Chat — {skill}").format(skill=skill.name)
		super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)

		self._provider = provider
		self._model = model
		self._skill = skill
		self._image_base64 = image_base64
		self._messages: list[dict] = []
		self._temperature = temperature
		self._recording = False
		self._stop_recording = threading.Event()
		self._has_initial_exchange = initial_exchange is not None

		# Load all skills for the dropdown.
		self._all_skills = load_skills()

		# If a skill has a system-level prompt, inject it as first message.
		# Skip when pre-seeding a follow-up: the seeded user message already
		# carries the rendered skill prompt verbatim, so a system copy would
		# just duplicate the instructions.
		if skill and skill.prompt and initial_exchange is None:
			rendered = skill.render_prompt()
			self._messages.append({"role": "system", "content": rendered})

		# If clipboard text is provided, add it as context.
		if clipboard_text:
			# Translators: System message providing clipboard context to the AI.
			context = _("The user has shared the following clipboard content:\n\n{text}").format(
				text=clipboard_text
			)
			self._messages.append({"role": "system", "content": context})

		# Pre-seed a prior (user, assistant) exchange for follow-up questions.
		if initial_exchange is not None:
			prior_user, prior_assistant = initial_exchange
			self._messages.append({"role": "user", "content": prior_user})
			self._messages.append({"role": "assistant", "content": prior_assistant})

		self._build_ui()

		# Display the pre-seeded exchange in the history so the user can read
		# back what was asked / answered before typing a follow-up.
		if initial_exchange is not None:
			prior_user, prior_assistant = initial_exchange
			self._history.AppendText(_("You: ") + prior_user + "\n")
			self._history.AppendText(_("AI: ") + prior_assistant + "\n")

		self.CentreOnScreen()

		# Announce the active skill after the dialog is shown.
		skill_name = skill.name if skill else _("None")
		# Translators: Spoken when the text chat dialog opens. {skill} is the active skill.
		wx.CallAfter(ui.message, _("Currently active: {skill}").format(skill=skill_name))

	def _build_ui(self) -> None:
		panel = wx.Panel(self)
		sizer = wx.BoxSizer(wx.VERTICAL)

		# -- Skill selector --
		# Translators: Label for the skill selector dropdown in chat.
		sizer.Add(wx.StaticText(panel, label=_("S&kill:")), flag=wx.LEFT | wx.TOP, border=10)
		skill_names = [sk.name for sk in self._all_skills]
		self._skill_choice = wx.Choice(panel, choices=skill_names)
		self._skill_choice.Bind(wx.EVT_CHOICE, self._on_skill_changed)

		# Pre-select the current skill.
		selected_idx = 0
		if self._skill:
			for i, sk in enumerate(self._all_skills):
				if sk.name == self._skill.name:
					selected_idx = i
					break
		self._skill_choice.SetSelection(selected_idx)
		sizer.Add(self._skill_choice, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# -- Chat history --
		# Translators: Label for the chat history field.
		sizer.Add(wx.StaticText(panel, label=_("&History:")), flag=wx.LEFT | wx.TOP, border=10)
		self._history = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
		sizer.Add(self._history, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# -- Message input --
		# Translators: Label for the chat input field.
		sizer.Add(wx.StaticText(panel, label=_("&Message:")), flag=wx.LEFT | wx.TOP, border=10)
		self._input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
		self._input.Bind(wx.EVT_TEXT_ENTER, self._on_send)
		sizer.Add(self._input, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

		# Translators: Button to send the message.
		self._send_btn = wx.Button(panel, label=_("&Send"))
		self._send_btn.Bind(wx.EVT_BUTTON, self._on_send)
		btn_sizer.Add(self._send_btn, flag=wx.RIGHT, border=8)

		# Translators: Button to start voice input via speech recognition.
		self._speech_btn = wx.Button(panel, label=_("S&peech"))
		self._speech_btn.Bind(wx.EVT_BUTTON, self._on_speech)
		btn_sizer.Add(self._speech_btn, flag=wx.RIGHT, border=8)

		# Translators: Button to close the chat dialog.
		close_btn = wx.Button(panel, id=wx.ID_CLOSE, label=_("Close"))
		close_btn.Bind(wx.EVT_BUTTON, lambda _e: self.Destroy())
		self.SetEscapeId(wx.ID_CLOSE)
		btn_sizer.Add(close_btn)

		sizer.Add(btn_sizer, flag=wx.ALIGN_RIGHT | wx.ALL, border=10)
		panel.SetSizer(sizer)

		self.SetSize((700, 500))
		self.SetMinSize((500, 350))
		self._input.SetFocus()

	# -- Skill switching -------------------------------------------------

	def _on_skill_changed(self, _event: wx.CommandEvent) -> None:
		"""Handle skill selection change from the dropdown."""
		idx = self._skill_choice.GetSelection()
		if idx == wx.NOT_FOUND or idx >= len(self._all_skills):
			return

		new_skill = self._all_skills[idx]
		self._skill = new_skill

		# Resolve provider + model for the new skill.
		provider, model = self._resolve_provider_and_model(new_skill)
		if provider is not None:
			self._provider = provider
			self._model = model

		# Resolve temperature.
		if new_skill.temperature_explicit:
			self._temperature = new_skill.temperature
		else:
			self._temperature = luma_config.get_setting("default_temperature", 0.3)

		# Update the system message (replace existing or insert new).
		new_prompt = new_skill.render_prompt() if new_skill.prompt else ""
		if self._messages and self._messages[0]["role"] == "system":
			if new_prompt:
				self._messages[0]["content"] = new_prompt
			else:
				self._messages.pop(0)
		elif new_prompt:
			self._messages.insert(0, {"role": "system", "content": new_prompt})

		# Update the dialog title.
		self.SetTitle(_("Luma Chat — {skill}").format(skill=new_skill.name))

		# Translators: Spoken when the skill is switched in the chat dialog.
		ui.message(_("Switched to {skill}").format(skill=new_skill.name))

	def _resolve_provider_and_model(self, skill: Skill):
		"""Resolve a provider instance and model string for *skill*.

		Uses the same resolution logic as GlobalPlugin but executed
		locally so the dialog does not depend on the plugin instance.
		Returns ``(provider, model)`` or ``(None, None)``.
		"""
		providers = luma_config.get_configured_providers()
		if not providers:
			return None, None

		settings = luma_config.get("settings") or {}

		# Determine provider name.
		provider_name = None
		if skill and skill.provider != "Default":
			provider_name = skill.provider
		if not provider_name:
			provider_name = settings.get("default_provider", "")

		# Find provider data by name, fall back to first available.
		pdata = None
		for p in providers:
			if p.get("name") == provider_name:
				pdata = p
				break
		if pdata is None:
			pdata = providers[0]

		# Determine model.
		model = ""
		if skill and skill.model != "Default":
			model = skill.model
		if not model:
			model = settings.get("default_model", "")
		if not model:
			return None, None

		try:
			provider = provider_from_dict(pdata)
		except Exception as exc:
			log.exception("Failed to create provider from config")
			# Translators: Spoken when provider creation fails. {error} is the detail.
			ui.message(_("Provider error: {error}").format(error=str(exc)))
			return None, None

		return provider, model

	# -- Chat logic --------------------------------------------------

	def _build_api_messages(self) -> list[dict]:
		"""Build the message list for the API call, applying truncation.

		Keeps all system messages, the first user message (which may carry
		the image), and the last ``_TRUNCATION_THRESHOLD`` non-system
		messages when the conversation exceeds that threshold.
		"""
		system_msgs = [m for m in self._messages if m["role"] == "system"]
		non_system = [m for m in self._messages if m["role"] != "system"]

		if len(non_system) <= _TRUNCATION_THRESHOLD:
			return list(self._messages)

		# Always keep the first user message (carries the image context).
		first_user = non_system[0] if non_system else None
		recent = non_system[-_TRUNCATION_THRESHOLD:]

		result = list(system_msgs)
		if first_user and first_user not in recent:
			result.append(first_user)
		result.extend(recent)
		return result

	def _on_send(self, _event: wx.CommandEvent) -> None:
		text = self._input.GetValue().strip()
		if not text:
			self._input.SetFocus()
			return

		self._input.SetValue("")
		self._send_btn.Enable(False)

		# Show user message.
		# Translators: Prefix for the user's message in chat history.
		self._history.AppendText(_("You: ") + text + "\n")
		ui.message(_("You: ") + text)

		# Show thinking indicator.
		# Translators: Indicator shown while waiting for the AI response.
		self._history.AppendText(_("AI: (thinking...)") + "\n")

		self._messages.append({"role": "user", "content": text})

		# Determine whether to attach the image. For a fresh chat, we attach
		# only on the very first user turn (the provider binds it to that
		# message). For a follow-up seeded with a prior exchange, every turn
		# re-attaches: the API is stateless, so the image must travel with
		# each call for the model to keep referencing it.
		image = None
		if self._image_base64:
			user_msg_count = sum(1 for m in self._messages if m["role"] == "user")
			if user_msg_count == 1 or self._has_initial_exchange:
				image = self._image_base64

		api_messages = self._build_api_messages()

		run_in_background(
			self._provider.process_request,
			kwargs={
				"model": self._model,
				"messages": api_messages,
				"temperature": self._temperature,
				"image_base64": image,
				"timeout": luma_config.get_setting("timeout", 60),
			},
			on_success=self._on_response,
			on_error=self._on_error,
		)

	def _on_response(self, response: str) -> None:
		# Remove "thinking..." line.
		current = self._history.GetValue()
		thinking = _("AI: (thinking...)") + "\n"
		if current.endswith(thinking):
			self._history.SetValue(current[: -len(thinking)])

		# Strip reasoning tags for both display and stored context.
		cleaned = _strip_reasoning(response)

		# Translators: Prefix for the AI's message in chat history.
		self._history.AppendText(_("AI: ") + cleaned + "\n")
		ui.message(cleaned)

		self._messages.append({"role": "assistant", "content": cleaned})
		self._send_btn.Enable(True)
		self._input.SetFocus()

	def _on_error(self, exc: Exception) -> None:
		# Remove "thinking..." line.
		current = self._history.GetValue()
		thinking = _("AI: (thinking...)") + "\n"
		if current.endswith(thinking):
			self._history.SetValue(current[: -len(thinking)])

		# Translators: Error message shown in chat history. {error} is the detail.
		self._history.AppendText(_("Error: {error}").format(error=str(exc)) + "\n")
		# Translators: Spoken error message for chat failures.
		ui.message(_("Error: {error}").format(error=str(exc)))

		self._send_btn.Enable(True)
		self._input.SetFocus()

	# -- Speech input --------------------------------------------------

	def _on_speech(self, _event: wx.CommandEvent) -> None:
		# Toggle: if already recording, signal stop.
		if self._recording:
			self._stop_recording.set()
			return

		from ..speech import is_microphone_available, record_and_transcribe

		if not self._provider.supports_transcription:
			# Translators: Spoken when the provider does not support transcription.
			ui.message(
				_("The current provider does not support speech transcription."),
			)
			return

		if not is_microphone_available():
			# Translators: Spoken when no microphone is detected.
			ui.message(_("No microphone found."))
			return

		self._recording = True
		self._stop_recording = threading.Event()
		# Translators: Button label while recording (Alt+P stops).
		self._speech_btn.SetLabel(_("Sto&p"))
		# Translators: Spoken when speech recognition starts listening.
		ui.message(_("Listening..."))

		run_in_background(
			record_and_transcribe,
			kwargs={
				"provider": self._provider,
				"model": self._model,
				"stop_event": self._stop_recording,
			},
			on_success=self._on_speech_result,
			on_error=self._on_speech_error,
			silent=True,
		)

	def _on_speech_done(self) -> None:
		"""Reset speech button state after recording finishes."""
		self._recording = False
		# Translators: Button to start voice input via speech recognition.
		self._speech_btn.SetLabel(_("S&peech"))

	def _on_speech_result(self, text: str) -> None:
		try:
			self._on_speech_done()
			current = self._input.GetValue()
			if current:
				self._input.SetValue(current + " " + text)
			else:
				self._input.SetValue(text)
			self._input.SetInsertionPointEnd()
			self._input.SetFocus()
			ui.message(text)
		except RuntimeError:
			# Dialog was closed while recording — ignore safely.
			pass

	def _on_speech_error(self, exc: Exception) -> None:
		try:
			self._on_speech_done()
			# Translators: Spoken when speech recognition fails. {error} is the detail.
			ui.message(_("Speech recognition failed: {error}").format(error=str(exc)))
			self._input.SetFocus()
		except RuntimeError:
			# Dialog was closed while recording — ignore safely.
			pass
