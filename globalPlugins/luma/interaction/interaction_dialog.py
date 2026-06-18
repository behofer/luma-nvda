"""Interaction Mode dialog for Luma.

Provides a simple interface where the user types a request and an
Anthropic Computer Use agentic loop autonomously executes it.  An
activity log shows real-time progress.
"""

from __future__ import annotations

import logging
import threading

import wx

import ui

from ..worker import run_in_background

log = logging.getLogger(__name__)


class InteractionDialog(wx.Dialog):
	"""Dialog for AI-controlled interaction mode.

	The user types a request (e.g. "Check the Privacy checkbox and
	press OK"), then clicks Start.  Claude takes screenshots, executes
	mouse/keyboard actions, and reports progress in the activity log.
	"""

	def __init__(
		self,
		parent: wx.Window | None,
		provider,
		model: str,
	) -> None:
		# Translators: Title of the Interaction Mode dialog.
		super().__init__(
			parent,
			title=_("Luma Interaction Mode"),
			style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
		)
		self._provider = provider
		self._model = model
		self._cancel_event: threading.Event | None = None
		self._running = False
		self._recording = False
		self._stop_recording = threading.Event()

		self._build_ui()
		self.CentreOnScreen()

	def _build_ui(self) -> None:
		panel = wx.Panel(self)
		sizer = wx.BoxSizer(wx.VERTICAL)

		# -- Activity log --
		# Translators: Label for the activity log in Interaction Mode.
		sizer.Add(
			wx.StaticText(panel, label=_("&Activity Log:")),
			flag=wx.LEFT | wx.TOP,
			border=10,
		)
		self._log = wx.TextCtrl(
			panel,
			style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
		)
		sizer.Add(
			self._log,
			proportion=1,
			flag=wx.EXPAND | wx.LEFT | wx.RIGHT,
			border=10,
		)

		# -- Request input --
		# Translators: Label for the request input in Interaction Mode.
		sizer.Add(
			wx.StaticText(panel, label=_("&Request:")),
			flag=wx.LEFT | wx.TOP,
			border=10,
		)
		self._input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
		self._input.Bind(wx.EVT_TEXT_ENTER, self._on_start)
		sizer.Add(
			self._input,
			flag=wx.EXPAND | wx.LEFT | wx.RIGHT,
			border=10,
		)

		# -- Buttons --
		btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

		# Translators: Button to start the interaction loop.
		self._start_btn = wx.Button(panel, label=_("&Start"))
		self._start_btn.Bind(wx.EVT_BUTTON, self._on_start)
		btn_sizer.Add(self._start_btn, flag=wx.RIGHT, border=8)

		# Translators: Button to start voice input via speech recognition.
		self._speech_btn = wx.Button(panel, label=_("S&peech"))
		self._speech_btn.Bind(wx.EVT_BUTTON, self._on_speech)
		btn_sizer.Add(self._speech_btn, flag=wx.RIGHT, border=8)

		# Translators: Button to close the Interaction Mode dialog.
		close_btn = wx.Button(panel, id=wx.ID_CLOSE, label=_("Close"))
		close_btn.Bind(wx.EVT_BUTTON, self._on_close)
		self.SetEscapeId(wx.ID_CLOSE)
		btn_sizer.Add(close_btn)

		sizer.Add(btn_sizer, flag=wx.ALIGN_RIGHT | wx.ALL, border=10)
		panel.SetSizer(sizer)

		self.SetSize((700, 500))
		self.SetMinSize((500, 350))
		self._input.SetFocus()

	# -- Start / Cancel ----------------------------------------------------

	def _on_start(self, _event: wx.CommandEvent) -> None:
		if self._running:
			# Already running — treat as Cancel.
			self._on_cancel()
			return

		request = self._input.GetValue().strip()
		if not request:
			self._input.SetFocus()
			return

		# Validate that the provider supports interaction mode.
		if not self._provider.supports_interaction:
			# Translators: Error when the provider doesn't support interaction.
			ui.message(
				_(
					"The current provider does not support Interaction Mode. "
					"Please use Anthropic or Gemini."
				)
			)
			return

		self._running = True
		self._cancel_event = threading.Event()
		self._input.Enable(False)
		# Translators: Button label while interaction is running.
		self._start_btn.SetLabel(_("&Cancel"))

		# Clear the log for a fresh run.
		self._log.SetValue("")

		# Capture the screen region before the dialog might get in the way.
		from ..capture import get_screen_region

		screen_region = get_screen_region()

		# Hide the dialog so screenshots capture the real desktop.
		self.Hide()

		from .agent_loop import InteractionLoop

		loop = InteractionLoop(
			self._provider,
			model=self._model,
			user_request=request,
			screen_region=screen_region,
			on_step=self._append_log,
			on_complete=self._on_loop_complete,
			on_error=self._on_loop_error,
			cancel_event=self._cancel_event,
		)

		run_in_background(loop.run)

	def _on_cancel(self) -> None:
		if self._cancel_event is not None:
			self._cancel_event.set()
		# Translators: Spoken when the user cancels interaction mode.
		ui.message(_("Cancelling..."))

	# -- Callbacks from the loop (main thread via wx.CallAfter) ------------

	def _append_log(self, text: str) -> None:
		"""Append a line to the activity log and speak it."""
		try:
			self._log.AppendText(text + "\n")
			ui.message(text)
		except RuntimeError:
			pass  # Dialog was closed.

	def _on_loop_complete(self, result: str) -> None:
		"""Called when the loop finishes successfully."""
		try:
			self._append_log("---")
			self._append_log(result)
			self._reset_ui()
		except RuntimeError:
			pass

	def _on_loop_error(self, error: str) -> None:
		"""Called when the loop encounters an unrecoverable error."""
		try:
			# Translators: Error prefix in the interaction activity log.
			self._append_log(_("Error: {error}").format(error=error))
			self._reset_ui()
		except RuntimeError:
			pass

	def _reset_ui(self) -> None:
		"""Re-enable the UI after the loop finishes."""
		self._running = False
		self._cancel_event = None
		self._input.Enable(True)
		self._input.SetValue("")
		# Translators: Button to start the interaction loop.
		self._start_btn.SetLabel(_("&Start"))
		self._input.SetFocus()
		# Restore the dialog so the user can see results.
		if not self.IsBeingDeleted():
			self.Show()
			self.Raise()

	# -- Speech input ------------------------------------------------------

	def _on_speech(self, _event: wx.CommandEvent) -> None:
		"""Toggle voice input: record and transcribe into the request field."""
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
			pass

	def _on_speech_error(self, exc: Exception) -> None:
		try:
			self._on_speech_done()
			# Translators: Spoken when speech recognition fails. {error} is the detail.
			ui.message(_("Speech recognition failed: {error}").format(error=str(exc)))
			self._input.SetFocus()
		except RuntimeError:
			pass

	# -- Close -------------------------------------------------------------

	def _on_close(self, _event: wx.CommandEvent) -> None:
		if self._running and self._cancel_event is not None:
			self._cancel_event.set()
		self.Destroy()
