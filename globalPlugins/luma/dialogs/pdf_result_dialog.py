"""Paginated PDF result dialog for Luma.

Displays AI-converted PDF pages one at a time with Previous / Next
navigation.  Pages are processed on-demand and cached for instant
back-navigation.
"""

from __future__ import annotations

import logging
import os

import wx

import ui

from ..worker import run_in_background

log = logging.getLogger(__name__)


class PdfResultDialog(wx.Dialog):
	"""A paginated dialog for displaying AI-processed PDF pages."""

	def __init__(
		self,
		parent: wx.Window | None = None,
		*,
		file_path: str,
		page_count: int,
		provider,
		model: str,
		skill_prompt: str,
		temperature: float = 0.1,
		timeout: int = 60,
		render_fn=None,
		cleanup_fn=None,
	) -> None:
		"""
		Parameters
		----------
		render_fn:
			Optional callable ``(file_path, page_index) -> base64_str``
			used to render a page as an image.  Defaults to
			:func:`file_handler.pdf.render_page`.
		cleanup_fn:
			Optional callable invoked (no args) when the dialog is
			destroyed, e.g. to remove temporary files.
		"""
		self._file_path = file_path
		self._page_count = page_count
		self._current_page = 0  # zero-based
		self._page_cache: dict[int, str] = {}
		self._processing = False

		self._provider = provider
		self._model = model
		self._skill_prompt = skill_prompt
		self._temperature = temperature
		self._timeout = timeout
		self._render_fn = render_fn
		self._cleanup_fn = cleanup_fn

		filename = os.path.basename(file_path)
		# Translators: Title of the PDF result dialog.
		# {filename} is the PDF file name, {page} is the current page,
		# {total} is the total number of pages.
		title = _("Luma PDF — {filename} — Page {page}/{total}").format(
			filename=filename, page=1, total=page_count,
		)
		super().__init__(
			parent, title=title,
			style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
		)
		self._build_ui()
		self.CentreOnScreen()

	def _build_ui(self) -> None:
		panel = wx.Panel(self)
		sizer = wx.BoxSizer(wx.VERTICAL)

		# -- Result text area --
		self._output = wx.TextCtrl(
			panel, value="",
			style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
		)
		sizer.Add(self._output, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

		# -- Button row --
		btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

		# Translators: Button to go to the previous PDF page.
		self._prev_btn = wx.Button(panel, label=_("&Previous page"))
		self._prev_btn.Bind(wx.EVT_BUTTON, self._on_previous)
		self._prev_btn.Enable(False)
		btn_sizer.Add(self._prev_btn, flag=wx.RIGHT, border=8)

		# Translators: Button to go to the next PDF page.
		self._next_btn = wx.Button(panel, label=_("&Next page"))
		self._next_btn.Bind(wx.EVT_BUTTON, self._on_next)
		self._next_btn.Enable(self._page_count > 1)
		btn_sizer.Add(self._next_btn, flag=wx.RIGHT, border=8)

		# Translators: Button to copy the result text to the clipboard.
		copy_btn = wx.Button(panel, label=_("&Copy"))
		copy_btn.Bind(wx.EVT_BUTTON, self._on_copy)
		btn_sizer.Add(copy_btn, flag=wx.RIGHT, border=8)

		# Translators: Button to close the dialog.
		close_btn = wx.Button(panel, id=wx.ID_CLOSE, label=_("Close"))
		close_btn.Bind(wx.EVT_BUTTON, self._on_close)
		self.SetEscapeId(wx.ID_CLOSE)
		btn_sizer.Add(close_btn)

		sizer.Add(btn_sizer, flag=wx.ALIGN_RIGHT | wx.ALL, border=10)
		panel.SetSizer(sizer)

		self.SetSize((750, 500))
		self.SetMinSize((500, 350))
		self._output.SetFocus()

	# -- Public API --------------------------------------------------------

	def navigate_to_page(self, page: int) -> None:
		"""Navigate to a specific page (zero-based).

		If the page is already cached, the result is displayed immediately.
		Otherwise, the page is rendered and processed in the background.
		"""
		if page < 0 or page >= self._page_count:
			return
		if self._processing:
			return

		self._current_page = page
		self._update_title()
		self._update_buttons()

		if page in self._page_cache:
			self._display_result(self._page_cache[page])
			return

		# Process the page in the background.
		self._processing = True
		self._prev_btn.Enable(False)
		self._next_btn.Enable(False)

		page_num = page + 1  # human-readable (1-based)
		# Translators: Shown while a PDF page is being processed.
		# {page} is the 1-based page number, {total} is the total.
		status = _("Processing page {page} of {total}...").format(
			page=page_num, total=self._page_count,
		)
		self._output.SetValue(status)
		ui.message(status)

		run_in_background(
			self._process_page,
			kwargs={"page": page},
			on_success=self._on_page_result,
			on_error=self._on_page_error,
		)

	# -- Internal ----------------------------------------------------------

	def _process_page(self, page: int) -> str:
		"""Render a document page and send it to the AI.

		Runs in a background thread.
		"""
		if self._render_fn is not None:
			image_b64 = self._render_fn(self._file_path, page)
		else:
			from ..file_handler.pdf import render_page
			image_b64 = render_page(self._file_path, page)
		messages = [{"role": "user", "content": self._skill_prompt}]
		return self._provider.process_request(
			model=self._model,
			messages=messages,
			temperature=self._temperature,
			image_base64=image_b64,
			timeout=self._timeout,
		)

	def _on_page_result(self, text: str) -> None:
		"""Handle a successful page result (main thread)."""
		self._processing = False
		self._page_cache[self._current_page] = text
		self._display_result(text)
		self._update_buttons()

	def _on_page_error(self, exc: Exception) -> None:
		"""Handle a page processing error (main thread)."""
		self._processing = False
		# Translators: Error shown when PDF page processing fails.
		# {error} is the error detail.
		error_text = _("Error processing page: {error}").format(error=str(exc))
		self._output.SetValue(error_text)
		ui.message(error_text)
		self._update_buttons()

	def _display_result(self, text: str) -> None:
		"""Show the result text and announce the page."""
		self._output.SetValue(text)
		self._output.SetInsertionPoint(0)
		self._output.SetFocus()

		page_num = self._current_page + 1
		# Translators: Spoken when a PDF page result is displayed.
		# {page} is the page number, {total} is the total.
		ui.message(_("Page {page} of {total}").format(
			page=page_num, total=self._page_count,
		))

	def _update_title(self) -> None:
		"""Update the dialog title with the current page number."""
		filename = os.path.basename(self._file_path)
		self.SetTitle(
			_("Luma PDF — {filename} — Page {page}/{total}").format(
				filename=filename,
				page=self._current_page + 1,
				total=self._page_count,
			)
		)

	def _update_buttons(self) -> None:
		"""Enable/disable navigation buttons based on current state."""
		self._prev_btn.Enable(self._current_page > 0 and not self._processing)
		self._next_btn.Enable(
			self._current_page < self._page_count - 1 and not self._processing
		)

	# -- Event handlers ----------------------------------------------------

	def _on_previous(self, _event: wx.CommandEvent) -> None:
		if self._current_page > 0:
			self.navigate_to_page(self._current_page - 1)

	def _on_next(self, _event: wx.CommandEvent) -> None:
		if self._current_page < self._page_count - 1:
			self.navigate_to_page(self._current_page + 1)

	def _on_close(self, _event: wx.CommandEvent) -> None:
		if self._cleanup_fn is not None:
			try:
				self._cleanup_fn()
			except Exception:
				log.debug("Cleanup failed on dialog close", exc_info=True)
		self.Destroy()

	def _on_copy(self, _event: wx.CommandEvent) -> None:
		if wx.TheClipboard.Open():
			wx.TheClipboard.SetData(wx.TextDataObject(self._output.GetValue()))
			wx.TheClipboard.Close()
			# Translators: Spoken when the result text has been copied to clipboard.
			ui.message(_("Copied to clipboard"))
