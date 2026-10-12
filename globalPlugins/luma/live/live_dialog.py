"""Live question dialog for Luma.

Opened by double-pressing the Live mode gesture (``NVDA+Shift+K``).
The user types (or dictates) a question such as *"Give me the slide
title and a short description of the slide in the shared screen"*.
Live mode then keeps taking screenshots and answers exactly that
question — tersely, and only when the answer changes.

The dialog hides itself while Live mode runs so it never ends up in
the screenshots.  Every spoken answer is also appended to the dialog's
log, so double-pressing the gesture again brings the dialog back for
re-reading, changing the question, or stopping.
"""

from __future__ import annotations

import logging
import threading

import wx

import ui

from ..worker import run_in_background

log = logging.getLogger(__name__)

# Delay between hiding the dialog and starting the first capture, so
# the desktop underneath has been repainted.
_HIDE_SETTLE_MS = 600


class LiveQuestionDialog(wx.Dialog):
	"""Ask a question that Live mode answers continuously."""

	def __init__(self, parent: wx.Window | None, plugin) -> None:
		# Translators: Title of the Live question dialog.
		super().__init__(
			parent,
			title=_("Luma Live — Ask a Question"),
			style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
		)
		self._plugin = plugin
		self._live = plugin._live_mode
		self._recording = False
		self._stop_recording = threading.Event()
		self._build_ui()
		self.CentreOnScreen()
		self._sync_state()

	# -- UI ------------------------------------------------------------------

	def _build_ui(self) -> None:
		panel = wx.Panel(self)
		sizer = wx.BoxSizer(wx.VERTICAL)

		# Translators: Label for the answers log in the Live question dialog.
		sizer.Add(wx.StaticText(panel, label=_("&Answers:")), flag=wx.LEFT | wx.TOP, border=10)
		self._log = wx.TextCtrl(
			panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
		)
		sizer.Add(self._log, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# Translators: Label for the question field in the Live question dialog.
		sizer.Add(wx.StaticText(panel, label=_("&Question to answer live:")), flag=wx.LEFT | wx.TOP, border=10)
		self._input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
		self._input.Bind(wx.EVT_TEXT_ENTER, self._on_start)
		sizer.Add(self._input, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)
		# Translators: Hint under the question field in the Live question dialog.
		sizer.Add(
			wx.StaticText(panel, label=_(
				"Example: Give me the slide title and a short description of the "
				"slide in the shared screen. Press Enter to start; the dialog hides "
				"while Live mode runs. Double-press the Live gesture to bring it back."
			)),
			flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10,
		)

		btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Button to start answering the question live.
		self._start_btn = wx.Button(panel, label=_("&Start"))
		self._start_btn.Bind(wx.EVT_BUTTON, self._on_start)
		btn_sizer.Add(self._start_btn, flag=wx.RIGHT, border=8)

		# Translators: Button to stop Live mode from the Live question dialog.
		self._stop_btn = wx.Button(panel, label=_("S&top"))
		self._stop_btn.Bind(wx.EVT_BUTTON, self._on_stop)
		btn_sizer.Add(self._stop_btn, flag=wx.RIGHT, border=8)

		# Translators: Button to dictate the question via speech recognition.
		self._speech_btn = wx.Button(panel, label=_("S&peech"))
		self._speech_btn.Bind(wx.EVT_BUTTON, self._on_speech)
		btn_sizer.Add(self._speech_btn, flag=wx.RIGHT, border=8)

		# Translators: Button to close the Live question dialog.
		close_btn = wx.Button(panel, id=wx.ID_CLOSE, label=_("Close"))
		close_btn.Bind(wx.EVT_BUTTON, self._on_close)
		self.SetEscapeId(wx.ID_CLOSE)
		btn_sizer.Add(close_btn)

		sizer.Add(btn_sizer, flag=wx.ALIGN_RIGHT | wx.ALL, border=10)
		panel.SetSizer(sizer)
		self.SetSize((700, 480))
		self.SetMinSize((500, 340))

	def _sync_state(self) -> None:
		"""Reflect the Live controller's state in the widgets."""
		running = self._live.is_running
		current = self._live.question or self._live.last_question
		if current and not self._input.GetValue():
			self._input.SetValue(current)
			self._input.SelectAll()
		self._stop_btn.Enable(running)
		if running and self._live.question:
			# Translators: Start button label while a live question is running.
			self._start_btn.SetLabel(_("&Change Question"))
		else:
			# Translators: Button to start answering the question live.
			self._start_btn.SetLabel(_("&Start"))
		self._input.SetFocus()

	def show_again(self) -> None:
		"""Bring a hidden dialog back (double-press while running)."""
		self._sync_state()
		self.Show()
		self.Raise()
		if self._live.is_running and self._live.question:
			# Translators: Spoken when the Live question dialog is reopened
			# while a question is running. {question} is the active question.
			ui.message(_("Live mode is answering: {question}").format(question=self._live.question))

	# -- Start / stop --------------------------------------------------------

	def _on_start(self, _event: wx.CommandEvent) -> None:
		question = self._input.GetValue().strip()
		if not question:
			self._input.SetFocus()
			return
		self._log.AppendText(
			# Translators: Log line when a live question (re)starts. {question} is the question.
			_("Question: {question}").format(question=question) + "\n",
		)
		# Hide first so the first screenshot shows the real screen.
		self.Hide()
		wx.CallLater(_HIDE_SETTLE_MS, self._start_after_hide, question)

	def _start_after_hide(self, question: str) -> None:
		if not self:
			return
		if self._live.is_running:
			self._live.refocus(question, on_answer=self._append_answer)
		else:
			self._live.start(question=question, on_answer=self._append_answer)
		if not self._live.is_running:
			# Start failed (provider/skill problem was already spoken).
			self.Show()
			self.Raise()
			self._sync_state()

	def _on_stop(self, _event: wx.CommandEvent) -> None:
		if self._live.is_running:
			self._live.stop()
		self._sync_state()

	def _append_answer(self, text: str) -> None:
		"""Main-thread callback from LiveMode for every spoken answer."""
		try:
			self._log.AppendText(text + "\n")
		except RuntimeError:
			pass  # Dialog destroyed.

	# -- Speech input ---------------------------------------------------------

	def _on_speech(self, _event: wx.CommandEvent) -> None:
		if self._recording:
			self._stop_recording.set()
			return

		from ..speech import is_microphone_available, record_and_transcribe
		from .live import LiveMode

		skill = LiveMode._find_live_skill()
		if skill is None:
			# Translators: Spoken when the Live skill is missing.
			ui.message(_("Live skill not found."))
			return
		provider, model = self._plugin._resolve_provider_and_model(skill)
		if provider is None:
			return
		if not provider.supports_transcription:
			# Translators: Spoken when the provider does not support transcription.
			ui.message(_("The current provider does not support speech transcription."))
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
			kwargs={"provider": provider, "model": model, "stop_event": self._stop_recording},
			on_success=self._on_speech_result,
			on_error=self._on_speech_error,
			silent=True,
		)

	def _on_speech_done(self) -> None:
		self._recording = False
		# Translators: Button to dictate the question via speech recognition.
		self._speech_btn.SetLabel(_("S&peech"))

	def _on_speech_result(self, text: str) -> None:
		try:
			self._on_speech_done()
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

	# -- Close ---------------------------------------------------------------

	def _on_close(self, _event: wx.CommandEvent) -> None:
		"""Close stops a running live question (like Interaction Mode's Close)."""
		if self._live.is_running and self._live.question:
			self._live.stop()
		self._plugin._live_dialog = None
		self.Destroy()
