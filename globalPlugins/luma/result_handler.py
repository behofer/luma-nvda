"""Result presentation for Luma.

Results are routed to one of two renderers:

1. **Default path** — ``ui.browseableMessage(html, title, isHtml=True)``.
   NVDA's built-in browseable message window: focus lands on the document
   in browse mode immediately, heading / link quick-nav works, Ctrl+C
   copies the rendered text. This is the preferred path for every result
   that doesn't need in-dialog click interception.

2. **Interaction path** — ``ResultDialog`` (``wx.html2.WebView``). Used
   only when a *screen_region* is supplied and the HTML contains
   ``data-coord`` markers. Activating a rewritten ``luma-click://x,y``
   link hides the dialog, issues a physical mouse click at the mapped
   screen location, then restores the dialog. ``ui.browseableMessage``
   cannot intercept link clicks — it opens them in the system browser —
   so we fall back to our own WebView only for this feature.

All functions MUST be called on the main thread (typically via
``wx.CallAfter`` from the worker).
"""

from __future__ import annotations

import html as _html
import logging
import re
import webbrowser

import wx

import ui

log = logging.getLogger(__name__)

# Has the one-time "WebView unavailable" notice already been spoken?
_degraded_announced = False


def show_result(
	text: str,
	*,
	title: str = "",
	screen_region: tuple[int, int, int, int] | None = None,
	skill_name: str | None = None,
) -> None:
	"""Present *text* to the user.

	Parameters
	----------
	text:
		The raw API response (markdown or HTML — both work).
	title:
		Window title for the result window. Titles starting with "Luma"
		are what the Alt+F follow-up overlay anchors on, so callers should
		keep that convention.
	screen_region:
		``(left, top, width, height)`` of the captured screen area.
		When provided **and** the HTML contains ``data-coord`` markers,
		the WebView ``ResultDialog`` is used so clicks can be intercepted
		and mapped to physical mouse clicks.
	skill_name:
		Active skill name — forwarded to the interaction dialog for the
		future Follow-up button. The default (browseable-message) path
		does not use it; the global Alt+F overlay reads last-exchange
		context from the ``GlobalPlugin`` instead.
	"""
	from . import config as luma_config

	if not title:
		# Translators: Default title for the Luma result window.
		title = _("Luma Result")
	elif not title.startswith("Luma"):
		# Callers typically pass skill.name (e.g. "Describe"). Prefix with
		# "Luma Result" so the window title reliably identifies our dialog
		# — and the Alt+F follow-up overlay can anchor on the prefix.
		# Translators: Result window title. {title} is the skill name.
		title = _("Luma Result — {title}").format(title=title)

	open_in_dialog = luma_config.get_setting("open_in_dialog", True)
	html = _transform_html_for_interaction(_to_html(text))
	needs_interaction = screen_region is not None and "luma-click://" in html

	if not open_in_dialog and not needs_interaction:
		ui.message(text)
		return

	if needs_interaction:
		_open_dialog(text, title, screen_region, skill_name)
		return

	# Default path: NVDA's built-in browseable message. Immediate browse
	# mode on open, Ctrl+C to copy, heading / link quick-nav — the
	# canonical NVDA screen-reader UX.
	ui.browseableMessage(_BASE_STYLE + html, title, True)


def show_error(error: Exception | str, *, title: str = "") -> None:
	"""Speak an error message. ``title`` is reserved for future use."""
	if not title:
		# Translators: Default title for a Luma error message.
		title = _("Luma Error")

	message = str(error)
	# Translators: Spoken when an API request fails. {error} is the error detail.
	ui.message(_("Error: {error}").format(error=message))
	log.error("Luma error shown to user: %s", message)


# ---------------------------------------------------------------------------
# Markdown → HTML
# ---------------------------------------------------------------------------


def _to_html(text: str) -> str:
	"""Render *text* as HTML via the bundled ``markdown`` module.

	Python-markdown passes raw HTML through unchanged, so the same
	function handles both markdown-emitting skills and the ``html``
	skill (which emits pre-rendered HTML with ``data-coord``
	attributes).

	On failure, fall back to an escaped ``<pre>`` block so the user
	still sees something instead of a blank dialog.
	"""
	try:
		import markdown
		from markdown.extensions import fenced_code, nl2br, sane_lists, tables

		return markdown.markdown(
			text,
			extensions=[
				fenced_code.FencedCodeExtension(),
				tables.TableExtension(),
				nl2br.Nl2BrExtension(),
				sane_lists.SaneListExtension(),
			],
			output_format="html5",
		)
	except Exception:
		log.exception("Markdown conversion failed; falling back to <pre>")
		return f"<pre>{_html.escape(text)}</pre>"


# ---------------------------------------------------------------------------
# data-coord → luma-click:// transform (unchanged logic)
# ---------------------------------------------------------------------------

_COORD_ELEMENT_RE = re.compile(
	r'<(\w+)([^>]*?)\bdata-coord="\[(\d+),\s*(\d+)\]"([^>]*)>(.*?)</\1>',
	re.DOTALL,
)

_VOID_COORD_RE = re.compile(
	r'<(input|img)\b([^>]*?)\bdata-coord="\[(\d+),\s*(\d+)\]"([^>]*)/?>'
)


def _transform_html_for_interaction(html: str) -> str:
	"""Rewrite ``data-coord`` elements as ``luma-click://x,y`` links.

	If the HTML contains no ``data-coord`` markers this is a pure
	no-op, so we can safely run it on every response.
	"""

	def _replace_element(match: re.Match) -> str:
		x, y = match.group(3), match.group(4)
		content = match.group(6).strip()
		label = re.sub(r'<[^>]+>', '', content).strip()
		if not label:
			# Translators: Fallback label for an interactive element with no text.
			label = _("Interact")
		return f'<a href="luma-click://{x},{y}">{label}</a>'

	result = _COORD_ELEMENT_RE.sub(_replace_element, html)

	def _replace_void(match: re.Match) -> str:
		x, y = match.group(3), match.group(4)
		attrs = match.group(2) + match.group(5)
		label_match = re.search(
			r'(?:aria-label|placeholder|alt|title|value)="([^"]*)"', attrs
		)
		if label_match:
			label = label_match.group(1)
		else:
			# Translators: Fallback label for an interactive element with no text.
			label = _("Interact")
		return f'<a href="luma-click://{x},{y}">{label}</a>'

	return _VOID_COORD_RE.sub(_replace_void, result)


# ---------------------------------------------------------------------------
# Unified ResultDialog
# ---------------------------------------------------------------------------

# Minimal CSS so the rendered page is comfortable to look at and
# renders code blocks / tables sensibly. Kept small on purpose — the
# goal is readability, not styling parity with chat.openai.com.
_BASE_STYLE = (
	"<style>"
	"body{font-family:'Segoe UI',sans-serif;font-size:14pt;line-height:1.4;padding:8px}"
	"code,pre{font-family:Consolas,monospace}"
	"pre{background:#f4f4f4;padding:6px;white-space:pre-wrap}"
	"table{border-collapse:collapse}"
	"td,th{border:1px solid #888;padding:4px}"
	"a{color:#0645ad}"
	"</style>"
)


class ResultDialog(wx.Dialog):
	"""HTML result dialog backed by ``wx.html2.WebView`` (with an
	``wx.html.HtmlWindow`` fallback for environments where the WebView
	backend cannot be instantiated).

	The dialog is deliberately extensible: ``skill_name`` is stored so
	a future "Follow-up question" action can open ``TextChatDialog``
	with the matching skill preselected. The Follow-up button exists
	but is hidden; wiring it up is a future change.
	"""

	def __init__(
		self,
		parent: wx.Window | None,
		title: str,
		raw_text: str,
		*,
		screen_region: tuple[int, int, int, int] | None = None,
		skill_name: str | None = None,
	) -> None:
		super().__init__(
			parent,
			title=title,
			style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
		)
		self._raw_text = raw_text
		self._screen_region = screen_region
		self._skill_name = skill_name
		self._backend = "webview"

		# Markdown → HTML → luma-click transform. Order matters:
		# running the transform AFTER markdown ensures any inserted
		# <a href="luma-click://"> anchor is not re-escaped by the
		# markdown processor.
		self._html_body = _transform_html_for_interaction(_to_html(raw_text))

		self._build_ui()
		self.CentreOnScreen()

	# -- UI construction ---------------------------------------------------

	def _build_ui(self) -> None:
		panel = wx.Panel(self)
		sizer = wx.BoxSizer(wx.VERTICAL)

		self._view = self._create_view(panel)
		sizer.Add(self._view, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

		self._load_page()
		self._bind_view_events()

		btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

		# Translators: Button to copy the result text to the clipboard.
		copy_btn = wx.Button(panel, label=_("&Copy"))
		copy_btn.Bind(wx.EVT_BUTTON, self._on_copy)
		btn_sizer.Add(copy_btn, flag=wx.RIGHT, border=8)

		# Extension point — hidden today, revealed by the future
		# follow-up feature. Kept in the sizer so the reveal is a
		# single ``.Show()`` call.
		# Translators: Button that will open a chat to ask a follow-up question.
		self._followup_btn = wx.Button(panel, label=_("&Follow-up question"))
		self._followup_btn.Bind(wx.EVT_BUTTON, self._on_followup)
		self._followup_btn.Hide()
		btn_sizer.Add(self._followup_btn, flag=wx.RIGHT, border=8)

		# Translators: Button to close the result dialog.
		close_btn = wx.Button(panel, id=wx.ID_CLOSE, label=_("Close"))
		close_btn.Bind(wx.EVT_BUTTON, self._on_close)
		self.SetEscapeId(wx.ID_CLOSE)
		btn_sizer.Add(close_btn)

		sizer.Add(btn_sizer, flag=wx.ALIGN_RIGHT | wx.ALL, border=10)
		panel.SetSizer(sizer)

		self.SetSize((800, 600))
		self.SetMinSize((500, 400))

	def _create_view(self, panel: wx.Panel):
		"""Build the HTML viewer, preferring ``wx.html2.WebView``.

		Falls back to ``wx.html.HtmlWindow`` when the WebView backend
		cannot be instantiated (stripped-down Windows images, missing
		Edge runtime with no IE fallback, etc.). In the fallback,
		browse-mode quick-nav is lost but everything else still works.
		"""
		try:
			import wx.html2

			view = wx.html2.WebView.New(panel)
			if view is None:
				raise RuntimeError("WebView.New returned None")
			self._backend = "webview"
			return view
		except Exception:
			log.warning(
				"wx.html2.WebView unavailable; using HtmlWindow fallback",
				exc_info=True,
			)
			import wx.html

			self._backend = "htmlwindow"
			global _degraded_announced
			if not _degraded_announced:
				_degraded_announced = True
				# Translators: Spoken once when the rich HTML dialog cannot be used.
				wx.CallAfter(
					ui.message, _("Rich view unavailable, using basic dialog")
				)
			return wx.html.HtmlWindow(panel)

	def _load_page(self) -> None:
		page = (
			"<!doctype html><html><head><meta charset='utf-8'>"
			f"{_BASE_STYLE}</head><body>{self._html_body}</body></html>"
		)
		if self._backend == "webview":
			self._view.SetPage(page, "")
		else:
			self._view.SetPage(page)

	def _bind_view_events(self) -> None:
		if self._backend == "webview":
			import wx.html2

			self._view.Bind(wx.html2.EVT_WEBVIEW_NAVIGATING, self._on_navigating)
			self._view.Bind(wx.html2.EVT_WEBVIEW_LOADED, self._on_loaded)
			try:
				self._view.EnableContextMenu(False)
			except Exception:
				# Some backends do not implement EnableContextMenu; ignore.
				pass
		else:
			import wx.html

			self._view.Bind(wx.html.EVT_HTML_LINK_CLICKED, self._on_html_link_clicked)

	# -- WebView event handlers -------------------------------------------

	def _on_loaded(self, _event) -> None:
		"""Focus the view once the DOM is live so NVDA enters browse mode.

		Setting focus in ``_build_ui`` lands it on a blank document and
		NVDA will not offer heading / link quick-nav.
		"""
		if not self.IsBeingDeleted():
			self._view.SetFocus()

	def _on_navigating(self, event) -> None:
		"""Gate navigation — intercept ``luma-click://``, open external
		links in the default browser, allow internal ``SetPage`` loads.

		``SetPage`` itself dispatches ``about:blank`` / ``data:``
		navigations on both IE and Edge backends; a blanket veto would
		blank the page, so those schemes are explicitly allow-listed.
		"""
		url = event.GetURL() or ""
		if url.startswith("luma-click://"):
			event.Veto()
			self._handle_click(url)
			return
		if not url or url.startswith(("about:", "data:")):
			return
		if url.startswith(("http://", "https://", "mailto:")):
			event.Veto()
			try:
				webbrowser.open(url)
			except Exception:
				log.exception("Failed to open external link: %s", url)
			return
		event.Veto()

	def _on_html_link_clicked(self, event) -> None:
		"""HtmlWindow-fallback version of ``_on_navigating``."""
		url = event.GetLinkInfo().GetHref() or ""
		if url.startswith("luma-click://"):
			self._handle_click(url)
			return
		if url.startswith(("http://", "https://", "mailto:")):
			try:
				webbrowser.open(url)
			except Exception:
				log.exception("Failed to open external link: %s", url)
			return
		# Other schemes are ignored — HtmlWindow won't navigate anyway.

	# -- luma-click:// handling -------------------------------------------

	def _handle_click(self, url: str) -> None:
		try:
			coords = url[len("luma-click://"):]
			x_str, y_str = coords.split(",")
			norm_x = int(x_str.strip())
			norm_y = int(y_str.strip())
		except (ValueError, IndexError):
			log.warning("Invalid luma-click URL: %s", url)
			return

		if self._screen_region is None:
			log.warning("luma-click received but no screen_region on dialog")
			return

		# Hide so the click lands on the underlying UI, not this dialog.
		self.Hide()
		wx.CallLater(150, self._do_click, norm_x, norm_y)

	def _do_click(self, norm_x: int, norm_y: int) -> None:
		from .executor import perform_click

		perform_click(norm_x, norm_y, self._screen_region)
		wx.CallLater(500, self._restore)

	def _restore(self) -> None:
		if not self.IsBeingDeleted():
			self.Show()
			self.Raise()

	# -- Button handlers ---------------------------------------------------

	def _on_copy(self, _event: wx.CommandEvent) -> None:
		"""Copy the *original* text (markdown or HTML as supplied).

		Do NOT change this to copy the rendered DOM — users expect to
		paste raw markdown into other tools, and the raw text is also
		what the future Follow-up action will seed into chat.
		"""
		if wx.TheClipboard.Open():
			wx.TheClipboard.SetData(wx.TextDataObject(self._raw_text))
			wx.TheClipboard.Close()
			# Translators: Spoken when the result text has been copied to clipboard.
			ui.message(_("Copied to clipboard"))

	def _on_followup(self, _event: wx.CommandEvent) -> None:
		"""Placeholder for the future Alt+F follow-up action.

		When wired up, this will open ``TextChatDialog`` with
		``skill=<resolved by self._skill_name>`` and
		``clipboard_text=self._raw_text`` so the user can ask follow-up
		questions about the current result. The button is hidden today
		so this handler never fires.
		"""
		pass

	def _on_close(self, _event: wx.CommandEvent) -> None:
		self.Destroy()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _open_dialog(
	text: str,
	title: str,
	screen_region: tuple[int, int, int, int] | None,
	skill_name: str | None,
) -> None:
	gui_frame = None
	try:
		import gui

		gui_frame = gui.mainFrame
	except Exception:
		pass
	dlg = ResultDialog(
		gui_frame,
		title,
		text,
		screen_region=screen_region,
		skill_name=skill_name,
	)
	dlg.Show()
	dlg.Raise()
