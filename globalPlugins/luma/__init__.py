"""Luma - AI Companion for NVDA.

This global plugin provides blind users with AI-powered screen description,
text recognition, and chat through customizable skills and multiple AI providers.
"""

import logging
import os
import sys
import threading

import addonHandler
import api as nvda_api
import globalPluginHandler
import gui as nvda_gui
import scriptHandler
import ui
import wx

log = logging.getLogger(__name__)

# Add bundled third-party libraries to the import path.
_LIB_DIR = os.path.join(os.path.dirname(__file__), "lib")
if _LIB_DIR not in sys.path:
	sys.path.insert(0, _LIB_DIR)

# PyMuPDF ships native DLLs (mupdfcpp.dll) inside its package directory.
# Python 3.8+ no longer searches PATH or sys.path for dependent DLLs —
# we must register the directory explicitly so the .pyd files can load.
_PYMUPDF_DIR = os.path.join(_LIB_DIR, "pymupdf")
if os.path.isdir(_PYMUPDF_DIR):
	os.add_dll_directory(_PYMUPDF_DIR)

# NVDA ships wxPython without wx.html2. We bundle the matching binaries
# (_html2.pyd + wxmsw32u_webview + WebView2Loader) from the exact
# wxPython release NVDA is built against (4.2.4, cp313, win_amd64).
# The three steps below make `import wx.html2` succeed:
#   1. os.add_dll_directory so the .pyd can find its sibling DLLs.
#   2. PATH prepend as a belt-and-braces backup (some loaders ignore 1).
#   3. Extend wx.__path__ so the import machinery finds html2.py.
# Any failure is logged and swallowed — ResultDialog falls back to
# wx.html.HtmlWindow at runtime if wx.html2 turns out to be unloadable.
_WX_HTML2_DIR = os.path.join(_LIB_DIR, "wx_html2", "wx")
_wx_html2_dll_cookie = None
if os.path.isdir(_WX_HTML2_DIR):
	try:
		# Keep the cookie on the module so the directory stays registered
		# for the lifetime of the add-on (losing the handle can drop the entry).
		_wx_html2_dll_cookie = os.add_dll_directory(_WX_HTML2_DIR)
		os.environ["PATH"] = _WX_HTML2_DIR + os.pathsep + os.environ.get("PATH", "")
		if _WX_HTML2_DIR not in wx.__path__:
			wx.__path__.append(_WX_HTML2_DIR)
	except Exception:
		log.exception("Failed to register bundled wx.html2; falling back to wx.html.HtmlWindow")

try:
	addonHandler.initTranslation()
except addonHandler.AddonError:
	log.warning(
		"Could not initialise translations. "
		"Is this add-on running from NVDA's scratchpad directory?"
	)

from .gesture import DoublePress, GESTURE_MAP

from NVDAObjects import NVDAObject


class _LumaResultOverlay(NVDAObject):
	"""NVDAObject overlay that binds Alt+F → follow-up chat.

	``GlobalPlugin.chooseNVDAObjectOverlayClasses`` injects this class on
	any object whose ancestor dialog's title starts with "Luma". That
	scopes the binding to Luma result windows (browseable messages and
	``ResultDialog``) — Alt+F outside is unaffected.

	Inherits from ``NVDAObject`` so it slots cleanly into the MRO of
	whatever concrete NVDAObject subclass the focused element already is
	(``IAccessible``, ``UIA``, …). NVDA's ``ScriptableObject`` machinery
	then picks up ``@script``-decorated methods and binds their gestures.
	"""

	# Translators: Description for the Alt+F follow-up script, shown in
	# NVDA's Input Gestures dialog.
	@scriptHandler.script(
		description=_("Open a follow-up chat seeded with the current result"),
		gestures=["kb:alt+f"],
	)
	def script_lumaFollowup(self, gesture):
		# Look up the running plugin instance. The overlay has no direct
		# reference to it, and stashing module-level state would duplicate
		# what the plugin already tracks.
		for plugin in globalPluginHandler.runningPlugins:
			if isinstance(plugin, GlobalPlugin):
				wx.CallAfter(plugin._open_followup)
				return


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	"""Luma global plugin — AI companion for NVDA."""

	# Translators: Category name shown in NVDA Input Gestures dialog.
	scriptCategory = _("Luma")

	def __init__(self):
		super().__init__()
		from . import config as luma_config
		luma_config.load()

		# Double-press handlers for vision gestures.
		self._nav_press = DoublePress(self._on_nav_tap)
		self._screen_press = DoublePress(self._on_screen_tap)
		self._app_press = DoublePress(self._on_app_tap)
		self._clipboard_press = DoublePress(self._on_clipboard_tap)
		self._camera_press = DoublePress(self._on_camera_tap)

		# Cancellation tracking — set by the stop gesture.
		self._processing_cancel: threading.Event | None = None
		self._interaction_dialog = None

		# Live mode controller — lazily starts a tick loop on toggle.
		from .live import LiveMode
		self._live_mode = LiveMode(self)

		# Most recent (skill, image, prompt, response) tuple — stashed
		# whenever a result is shown, read by the Alt+F follow-up script
		# (see _LumaResultOverlay) to seed TextChatDialog.
		self._last_exchange: dict | None = None

		# Inject Luma submenu into the NVDA Tools menu.
		self._tools_menu = None
		self._tools_menu_item = None
		wx.CallAfter(self._create_tools_menu)

		log.info("Luma add-on initialised.")

	def terminate(self):
		from . import config as luma_config
		luma_config.save()
		# Stop any active Live session so its timer doesn't keep
		# firing after the plugin is unloaded.
		try:
			self._live_mode.stop()
		except Exception:
			log.exception("Error stopping live mode")
		self._remove_tools_menu()
		# Stop the local llama-server subprocess if one is running.
		try:
			from .local_inference import shutdown as _local_shutdown
			_local_shutdown()
		except Exception:
			log.exception("Error stopping local inference server")
		log.info("Luma add-on terminated.")

	# ==================================================================
	# Tools menu injection
	# ==================================================================

	def _create_tools_menu(self):
		"""Inject Luma submenu into NVDA's Tools menu."""
		self._tools_menu = wx.Menu()

		# Translators: Tools menu item to manage providers.
		item = self._tools_menu.Append(wx.ID_ANY, _("&Providers..."))
		nvda_gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._on_menu_providers, item)

		# Translators: Tools menu item to manage skills.
		item = self._tools_menu.Append(wx.ID_ANY, _("&Skills..."))
		nvda_gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._on_menu_skills, item)

		self._tools_menu.AppendSeparator()

		# Translators: Tools menu item to process navigator object.
		item = self._tools_menu.Append(wx.ID_ANY, _("Process Navigator &Object"))
		nvda_gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._on_menu_nav, item)

		# Translators: Tools menu item to process the selected file in File Explorer.
		item = self._tools_menu.Append(wx.ID_ANY, _("Process Selected &File"))
		nvda_gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._on_menu_file, item)

		# Translators: Tools menu item to process entire screen.
		item = self._tools_menu.Append(wx.ID_ANY, _("Process Entire &Screen"))
		nvda_gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._on_menu_screen, item)

		# Translators: Tools menu item to process current application.
		item = self._tools_menu.Append(wx.ID_ANY, _("Process Current &Application"))
		nvda_gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._on_menu_app, item)

		# Translators: Tools menu item to process clipboard content.
		item = self._tools_menu.Append(wx.ID_ANY, _("Process &Clipboard"))
		nvda_gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._on_menu_clipboard, item)

		# Translators: Tools menu item to process camera image.
		item = self._tools_menu.Append(wx.ID_ANY, _("Process Ca&mera"))
		nvda_gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._on_menu_camera, item)

		# Translators: Tools menu item to analyze a video URL.
		item = self._tools_menu.Append(wx.ID_ANY, _("Analyze &Video URL..."))
		nvda_gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._on_menu_video, item)

		# Translators: Tools menu item to open text chat.
		item = self._tools_menu.Append(wx.ID_ANY, _("&Text Chat"))
		nvda_gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._on_menu_chat, item)

		# Translators: Tools menu item to open interaction mode.
		item = self._tools_menu.Append(wx.ID_ANY, _("&Interaction Mode"))
		nvda_gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._on_menu_interaction, item)

		self._tools_menu.AppendSeparator()

		# Translators: Tools menu item to configure shortcuts.
		item = self._tools_menu.Append(wx.ID_ANY, _("S&hortcuts..."))
		nvda_gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._on_menu_shortcuts, item)

		# Translators: Tools menu item to open settings.
		item = self._tools_menu.Append(wx.ID_ANY, _("S&ettings..."))
		nvda_gui.mainFrame.sysTrayIcon.Bind(wx.EVT_MENU, self._on_menu_settings, item)

		# Insert Luma submenu into the NVDA Tools menu.
		self._tools_menu_item = nvda_gui.mainFrame.sysTrayIcon.toolsMenu.AppendSubMenu(
			self._tools_menu,
			# Translators: Label for the Luma submenu in NVDA's Tools menu.
			_("&Luma"),
		)

	def _remove_tools_menu(self):
		"""Remove Luma submenu from NVDA's Tools menu."""
		if self._tools_menu_item is not None:
			try:
				nvda_gui.mainFrame.sysTrayIcon.toolsMenu.Remove(self._tools_menu_item)
			except Exception:
				log.debug("Could not remove Luma tools menu item.", exc_info=True)
			self._tools_menu_item = None
			self._tools_menu = None

	# Tools menu event handlers.

	def _on_menu_providers(self, _evt):
		from .dialogs import ProvidersDialog
		wx.CallAfter(lambda: ProvidersDialog(nvda_gui.mainFrame).Show())

	def _on_menu_skills(self, _evt):
		from .dialogs import SkillsDialog
		wx.CallAfter(lambda: SkillsDialog(nvda_gui.mainFrame).Show())

	def _on_menu_nav(self, _evt):
		skill = self._require_active_skill()
		if skill:
			self._execute_skill_with_scope(skill, "navigator")

	def _on_menu_file(self, _evt):
		from . import file_handler

		file_path = file_handler.get_explorer_selected_file()
		if not file_path:
			# Translators: Spoken when no file is selected in File Explorer.
			ui.message(_("No file selected in File Explorer."))
			return
		skill = self._require_active_skill()
		if skill:
			self._process_file_with_skill(skill, file_path)

	def _on_menu_screen(self, _evt):
		skill = self._require_active_skill()
		if skill:
			self._execute_skill_with_scope(skill, "screen")

	def _on_menu_app(self, _evt):
		skill = self._require_active_skill()
		if skill:
			self._execute_skill_with_scope(skill, "app")

	def _on_menu_clipboard(self, _evt):
		from .clipboard import get_clipboard_content

		content_type, data = get_clipboard_content()
		if content_type == "empty":
			# Translators: Spoken when the clipboard is empty.
			ui.message(_("Clipboard is empty."))
			return
		skill = self._require_active_skill()
		if skill:
			if content_type == "image":
				# Translators: Source label for clipboard image processing.
				self._execute_skill_with_scope(skill, "navigator", image_base64=data, source_label=_("clipboard image"))
			else:
				self._process_text_with_skill(skill, data)

	def _on_menu_camera(self, _evt):
		skill = self._require_active_skill()
		if skill:
			self._capture_and_process_camera(skill, open_chat=False)

	def _on_menu_video(self, _evt):
		wx.CallAfter(self._process_video)

	def _on_menu_chat(self, _evt):
		self._open_text_chat()

	def _on_menu_interaction(self, _evt):
		wx.CallAfter(self._open_interaction_mode)

	def _on_menu_shortcuts(self, _evt):
		from .dialogs import ShortcutsDialog
		wx.CallAfter(lambda: ShortcutsDialog(nvda_gui.mainFrame).ShowModal())

	def _on_menu_settings(self, _evt):
		from .dialogs import SettingsDialog
		wx.CallAfter(lambda: SettingsDialog(nvda_gui.mainFrame).ShowModal())

	# ==================================================================
	# Helper methods
	# ==================================================================

	def _resolve_provider_and_model(self, skill=None):
		"""Resolve a provider instance and model string.

		Uses the skill's provider/model if set, otherwise falls back
		to the global defaults from settings.  Returns ``(provider, model)``
		or ``(None, None)`` if no provider can be resolved.
		"""
		from . import config as luma_config
		from .providers import provider_from_dict

		providers = luma_config.get_configured_providers()
		if not providers:
			# Translators: Spoken when no providers are configured.
			ui.message(_("No providers configured. Please add a provider first."))
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
			# Translators: Spoken when no model is configured.
			ui.message(_("No model configured. Please set a default model in Settings."))
			return None, None

		try:
			provider = provider_from_dict(pdata)
		except Exception as exc:
			log.exception("Failed to create provider from config")
			# Translators: Spoken when provider creation fails. {error} is the detail.
			ui.message(_("Provider error: {error}").format(error=str(exc)))
			return None, None

		return provider, model

	def _get_active_skill(self):
		"""Return the active :class:`Skill` or ``None``."""
		from . import config as luma_config
		from .skill import load_skills

		active_name = luma_config.get("active_skill")
		if not active_name:
			return None
		for sk in load_skills():
			if sk.name == active_name:
				return sk
		return None

	def _resolve_temperature(self, skill=None):
		"""Return the temperature to use for *skill*.

		Uses the skill's explicit temperature if set, otherwise the
		global default from settings.
		"""
		if skill and skill.temperature_explicit:
			return skill.temperature
		from . import config as luma_config
		return luma_config.get_setting("default_temperature", 0.3)

	def _require_active_skill(self):
		"""Return the active skill, speaking an error if none is set."""
		skill = self._get_active_skill()
		if skill is None:
			# Translators: Spoken when no active skill is set.
			ui.message(_("No active skill. Press NVDA+Shift+Space to select one."))
		return skill

	# Maps scope identifiers to user-friendly labels for processing messages.
	_SCOPE_LABELS = {
		"navigator": _("navigator object"),
		"screen": _("screen"),
		"app": _("application"),
	}

	def _execute_skill_with_scope(self, skill, scope, image_base64=None, source_label=None):
		"""Run *skill* against the given *scope*.

		*scope* is one of ``"navigator"``, ``"screen"``, ``"app"``,
		or ``"text_chat"``.

		*source_label* overrides the default scope label in the
		processing message (e.g. ``"file photo.jpg"``).
		"""
		from . import capture
		from . import config as luma_config

		if scope == "text_chat":
			self._open_text_chat(skill=skill, image_base64=image_base64)
			return

		# Capture image for the scope, along with the screen region
		# (used by the interaction feature to map VLM coordinates back
		# to absolute screen pixels for click execution).
		screen_region = None
		if image_base64 is None:
			if scope == "navigator":
				nav = nvda_api.getNavigatorObject()
				image_base64 = capture.capture_object(nav)
			elif scope == "screen":
				image_base64 = capture.capture_screen()
				screen_region = capture.get_screen_region()
			elif scope == "app":
				image_base64 = capture.capture_app()
				screen_region = capture.get_app_region()
				if screen_region is None:
					screen_region = capture.get_screen_region()

		if image_base64 is None:
			# Translators: Spoken when screen capture fails.
			ui.message(_("Failed to capture image."))
			return

		provider, model = self._resolve_provider_and_model(skill)
		if provider is None:
			return

		prompt = skill.render_prompt()

		label = source_label or self._SCOPE_LABELS.get(scope, scope)
		# Translators: Spoken when processing begins. {source} is what is being
		# processed (e.g. "navigator object", "screen", "file photo.jpg").
		# {skill} is the active skill name (e.g. "Describe").
		ui.message(_("Processing {source} using {skill}...").format(
			source=label, skill=skill.name,
		))

		from .worker import process_skill
		from .result_handler import show_result, show_error

		# Cancel any previously running processing task.
		if self._processing_cancel is not None:
			self._processing_cancel.set()
		self._processing_cancel = threading.Event()

		def _on_success(text):
			self._stash_last_exchange(
				skill=skill,
				image_base64=image_base64,
				prompt=prompt,
				response=text,
			)
			show_result(
				text,
				title=skill.name,
				screen_region=screen_region,
				skill_name=skill.name,
			)

		process_skill(
			provider,
			model=model,
			prompt=prompt,
			image_base64=image_base64,
			temperature=self._resolve_temperature(skill),
			timeout=luma_config.get_setting("timeout", 60),
			on_success=_on_success,
			on_error=show_error,
			cancel_event=self._processing_cancel,
		)

	def _stash_last_exchange(self, *, skill, image_base64, prompt, response):
		"""Record the most recent user-prompt / assistant-response pair.

		Called from every ``show_result`` callback so the Alt+F overlay
		can open a follow-up chat seeded with full context.
		"""
		self._last_exchange = {
			"skill": skill,
			"image_base64": image_base64,
			"prompt": prompt,
			"response": response,
		}

	def _open_followup(self):
		"""Open ``TextChatDialog`` pre-seeded with the last exchange.

		Invoked by ``_LumaResultOverlay.script_lumaFollowup`` (Alt+F while
		focus is inside a Luma result window). If nothing has been shown
		yet this session, speak a message and do nothing.
		"""
		if self._last_exchange is None:
			# Translators: Spoken when Alt+F is pressed with no prior result.
			ui.message(_("No recent Luma result to follow up on."))
			return

		ex = self._last_exchange
		skill = ex["skill"]
		provider, model = self._resolve_provider_and_model(skill)
		if provider is None:
			return

		from .dialogs import TextChatDialog
		dlg = TextChatDialog(
			parent=nvda_gui.mainFrame,
			provider=provider,
			model=model,
			skill=skill,
			image_base64=ex["image_base64"],
			temperature=self._resolve_temperature(skill),
			initial_exchange=(ex["prompt"], ex["response"]),
		)
		dlg.Show()
		dlg.Raise()

	# ------------------------------------------------------------------
	# Overlay binding — scopes Alt+F follow-up to Luma result windows only.
	# ------------------------------------------------------------------

	def chooseNVDAObjectOverlayClasses(self, obj, clsList):
		"""Attach the Luma result overlay whenever focus enters a Luma
		*result* window specifically.

		The overlay class binds ``kb:alt+f`` to ``script_lumaFollowup``.
		Because the binding lives on an overlay (not on ``GlobalPlugin``),
		it's active *only* while focus is inside the matched window — Alt+F
		in any other context (File-menu accelerator, chat dialog, etc.) is
		unaffected.

		We anchor on the ``"Luma Result"`` title prefix (set by
		``result_handler.show_result``) rather than a broader ``"Luma"``
		match — otherwise Alt+F would also fire inside Luma Chat / Settings
		/ Error dialogs, where a follow-up action is out of context.

		Identification is done via the Win32 window-handle hierarchy
		(``GetAncestor(GA_ROOT)`` + ``GetWindowText``) — *not* by walking
		``obj.parent``. Touching ``obj.parent`` here re-enters NVDAObject
		construction, which calls ``chooseNVDAObjectOverlayClasses`` again
		on every parent we visit, fanning out to a ``RecursionError``
		inside MSHTML documents (``ui.browseableMessage`` is one).
		"""
		handle = getattr(obj, "windowHandle", 0)
		if not handle:
			return
		try:
			import winUser
			root = winUser.getAncestor(handle, winUser.GA_ROOT)
			if not root:
				return
			title = winUser.getWindowText(root) or ""
		except Exception:
			# Any Win32 failure (closed window, permission, etc.) just
			# means we don't attach the overlay — never raise from here.
			log.debug("Luma overlay window-title probe failed", exc_info=True)
			return
		if title.startswith("Luma Result"):
			clsList.insert(0, _LumaResultOverlay)

	def _open_text_chat(self, *, skill=None, image_base64=None):
		"""Open the text chat dialog with the resolved provider.

		When *skill* is explicitly passed (e.g. from a menu action), its
		prompt is used as the chat system message.  When called from a
		double-press fallback (skill is None), the active skill is used
		only for provider/model resolution — its one-shot prompt is NOT
		injected, because it would override the user's first question.
		"""
		from .dialogs import TextChatDialog

		if image_base64:
			from .capture import optimize_image
			image_base64 = optimize_image(image_base64)

		# Track whether the caller explicitly chose a skill.
		caller_chose_skill = skill is not None

		# Resolve a skill for provider/model lookup.
		resolve_skill = skill
		if resolve_skill is None:
			resolve_skill = self._get_active_skill()
		if resolve_skill is None:
			from .skill import load_skills
			for sk in load_skills():
				if sk.name.lower() == "text chat":
					resolve_skill = sk
					caller_chose_skill = True  # "Text Chat" IS chat-appropriate
					break

		provider, model = self._resolve_provider_and_model(resolve_skill)
		if provider is None:
			return

		# Only pass the skill to the dialog when its prompt is
		# appropriate for chat (explicitly chosen or a "Text Chat" skill).
		dialog_skill = resolve_skill if caller_chose_skill else None

		dlg = TextChatDialog(
			parent=nvda_gui.mainFrame,
			provider=provider,
			model=model,
			skill=dialog_skill,
			image_base64=image_base64,
			temperature=self._resolve_temperature(resolve_skill),
		)
		dlg.Show()
		dlg.Raise()

	# ==================================================================
	# Popup menu
	# ==================================================================

	# Translators: Description for the open Luma menu gesture.
	@scriptHandler.script(description=_("Open Luma main menu"))
	def script_lumaMenu(self, gesture):
		wx.CallAfter(self._show_popup_menu)

	def _show_popup_menu(self):
		"""Build and show the Luma popup menu."""
		from . import config as luma_config
		from .skill import load_skills

		menu = wx.Menu()

		# Translators: Popup menu item to manage providers.
		item_providers = menu.Append(wx.ID_ANY, _("&Providers..."))
		# Translators: Popup menu item to manage skills.
		item_skills = menu.Append(wx.ID_ANY, _("&Skills..."))

		# Active Skill submenu.
		skills = load_skills()
		active_name = luma_config.get("active_skill")
		skill_submenu = wx.Menu()
		skill_ids = []
		for sk in skills:
			sid = wx.NewIdRef()
			item = skill_submenu.AppendRadioItem(sid, sk.name)
			if sk.name == active_name:
				item.Check(True)
			skill_ids.append((sid, sk.name))
		# Translators: Popup submenu label for selecting the active skill.
		menu.AppendSubMenu(skill_submenu, _("&Active Skill"))

		menu.AppendSeparator()

		# Translators: Popup menu item to process navigator object.
		item_nav = menu.Append(wx.ID_ANY, _("Process Navigator &Object"))
		# Translators: Popup menu item to process the selected file.
		item_file = menu.Append(wx.ID_ANY, _("Process Selected &File"))
		# Translators: Popup menu item to process entire screen.
		item_screen = menu.Append(wx.ID_ANY, _("Process Entire &Screen"))
		# Translators: Popup menu item to process current application.
		item_app = menu.Append(wx.ID_ANY, _("Process Current &Application"))
		# Translators: Popup menu item to process clipboard content.
		item_clipboard = menu.Append(wx.ID_ANY, _("Process &Clipboard"))
		# Translators: Popup menu item to process camera image.
		item_camera = menu.Append(wx.ID_ANY, _("Process Ca&mera"))
		# Translators: Popup menu item to analyze a video URL.
		item_video = menu.Append(wx.ID_ANY, _("Analyze &Video URL..."))
		# Translators: Popup menu item to open text chat.
		item_chat = menu.Append(wx.ID_ANY, _("&Text Chat"))
		# Translators: Popup menu item to open interaction mode.
		item_interaction = menu.Append(wx.ID_ANY, _("&Interaction Mode"))

		menu.AppendSeparator()

		# Translators: Popup menu item to configure shortcuts.
		item_shortcuts = menu.Append(wx.ID_ANY, _("S&hortcuts..."))
		# Translators: Popup menu item to open settings.
		item_settings = menu.Append(wx.ID_ANY, _("S&ettings..."))

		frame = nvda_gui.mainFrame

		def _on_skill_select(name):
			def handler(_evt):
				luma_config.set("active_skill", name)
				luma_config.save()
				# Translators: Spoken when the active skill is changed. {name} is the skill name.
				ui.message(_("Active skill: {name}").format(name=name))
			return handler

		frame.Bind(wx.EVT_MENU, lambda _e: self._on_menu_providers(_e), item_providers)
		frame.Bind(wx.EVT_MENU, lambda _e: self._on_menu_skills(_e), item_skills)
		frame.Bind(wx.EVT_MENU, lambda _e: self._on_menu_nav(_e), item_nav)
		frame.Bind(wx.EVT_MENU, lambda _e: self._on_menu_file(_e), item_file)
		frame.Bind(wx.EVT_MENU, lambda _e: self._on_menu_screen(_e), item_screen)
		frame.Bind(wx.EVT_MENU, lambda _e: self._on_menu_app(_e), item_app)
		frame.Bind(wx.EVT_MENU, lambda _e: self._on_menu_clipboard(_e), item_clipboard)
		frame.Bind(wx.EVT_MENU, lambda _e: self._on_menu_camera(_e), item_camera)
		frame.Bind(wx.EVT_MENU, lambda _e: self._on_menu_video(_e), item_video)
		frame.Bind(wx.EVT_MENU, lambda _e: self._on_menu_chat(_e), item_chat)
		frame.Bind(wx.EVT_MENU, lambda _e: self._on_menu_interaction(_e), item_interaction)
		frame.Bind(wx.EVT_MENU, lambda _e: self._on_menu_shortcuts(_e), item_shortcuts)
		frame.Bind(wx.EVT_MENU, lambda _e: self._on_menu_settings(_e), item_settings)

		for sid, name in skill_ids:
			frame.Bind(wx.EVT_MENU, _on_skill_select(name), id=sid)

		frame.prePopup()
		frame.PopupMenu(menu)
		frame.postPopup()
		menu.Destroy()

	# ==================================================================
	# Vision gestures — double-press detection via DoublePress
	# ==================================================================

	# -- Navigator object --

	# Translators: Description for the navigator object gesture.
	@scriptHandler.script(
		description=_("Process navigator object with active skill. Press twice quickly to open text chat."),
	)
	def script_processNavigator(self, gesture):
		self._nav_press.tap()

	def _on_nav_tap(self, count):
		from . import file_handler

		if count >= 2:
			# Double press — check for file first, then fall back to navigator.
			file_path = file_handler.get_explorer_selected_file()
			if file_path and file_handler.is_supported_file(file_path):
				ext = os.path.splitext(file_path)[1].lower()
				if ext in {".pptx", ".ppt"}:
					# PPTX conversion is slow — run in background.
					ui.message(_("Converting presentation..."))
					from .worker import run_in_background
					run_in_background(
						self._render_first_slide_for_chat,
						args=(file_path,),
						on_success=lambda b64: self._open_text_chat(image_base64=b64),
						on_error=lambda exc: ui.message(
							_("Could not read the selected file."),
						),
					)
				elif ext == ".pdf":
					from .file_handler.pdf import render_page
					try:
						image_b64 = render_page(file_path, 0)
					except Exception:
						ui.message(_("Could not read the selected file."))
						return
					self._open_text_chat(image_base64=image_b64)
				else:
					# Image files.
					image_base64 = file_handler.read_image_file(file_path)
					if image_base64:
						self._open_text_chat(image_base64=image_base64)
					else:
						ui.message(_("Could not read the selected file."))
			else:
				from . import capture
				nav = nvda_api.getNavigatorObject()
				image = capture.capture_object(nav)
				self._open_text_chat(image_base64=image)
		else:
			# Single press — check for file first, then fall back to navigator.
			skill = self._require_active_skill()
			if not skill:
				return
			file_path = file_handler.get_explorer_selected_file()
			if file_path:
				self._process_file_with_skill(skill, file_path)
			else:
				self._execute_skill_with_scope(skill, "navigator")

	def _process_file_with_skill(self, skill, file_path):
		"""Process a file selected in File Explorer with the given skill."""
		from . import file_handler

		if not file_handler.is_supported_file(file_path):
			ext = os.path.splitext(file_path)[1]
			# Translators: Spoken when an unsupported file type is selected.
			ui.message(_("File type {ext} is not supported.").format(ext=ext))
			return

		# Check file-size limit (exempt for page-by-page formats).
		if not file_handler.is_size_exempt(file_path):
			size = file_handler.get_file_size(file_path)
			if size is not None and size > file_handler._MAX_FILE_SIZE:
				# Translators: Spoken when the selected file is too large.
				ui.message(_("File is too large to process."))
				return

		# PPTX conversion (PowerPoint COM) can take several seconds.
		# Run it in the background to avoid blocking the main thread.
		ext = os.path.splitext(file_path)[1].lower()
		if ext in {".pptx", ".ppt"}:
			# Translators: Spoken while a presentation is being converted.
			ui.message(_("Converting presentation..."))
			from .worker import run_in_background
			run_in_background(
				file_handler.process_file,
				args=(file_path,),
				on_success=lambda result: self._on_pptx_converted(
					file_path, result,
				),
				on_error=lambda exc: ui.message(
					# Translators: Spoken when a file cannot be processed.
					_("Could not process file: {error}").format(error=str(exc)),
				),
			)
			return

		try:
			content_type, data = file_handler.process_file(file_path)
		except Exception as exc:
			# Translators: Spoken when a file cannot be processed.
			ui.message(_("Could not process file: {error}").format(error=str(exc)))
			return

		if content_type == "pdf":
			self._open_pdf_dialog(file_path, data["page_count"])
		elif content_type == "image":
			# Translators: Source label for file processing messages. {filename} is the file name.
			label = _("file {filename}").format(filename=os.path.basename(file_path))
			self._execute_skill_with_scope(
				skill, "navigator", image_base64=data, source_label=label,
			)
		else:
			# Translators: Spoken when no file could be read.
			ui.message(_("Could not read the selected file."))

	def _on_pptx_converted(self, file_path, result):
		"""Handle successful PPTX→PDF conversion (main thread callback)."""
		content_type, data = result
		self._open_pptx_dialog(file_path, data["pdf_path"], data["page_count"])

	@staticmethod
	def _render_first_slide_for_chat(file_path):
		"""Convert PPTX to PDF, render slide 1, clean up.  Background thread."""
		from .file_handler.pdf import render_page
		from .file_handler.pptx import cleanup_temp, process_pptx

		_content_type, data = process_pptx(file_path)
		pdf_path = data["pdf_path"]
		try:
			return render_page(pdf_path, 0)
		finally:
			cleanup_temp(pdf_path)

	def _open_pdf_dialog(self, file_path, page_count):
		"""Open the paginated PDF result dialog and auto-process page 1."""
		from . import config as luma_config
		from .dialogs import PdfResultDialog
		from .skill import load_skills

		# Find the builtin PDF Reader skill.
		pdf_skill = None
		for sk in load_skills():
			if sk.name == "PDF Reader":
				pdf_skill = sk
				break
		if pdf_skill is None:
			# Translators: Spoken when the PDF Reader skill is missing.
			ui.message(_("PDF Reader skill not found."))
			return

		provider, model = self._resolve_provider_and_model(pdf_skill)
		if provider is None:
			return

		dlg = PdfResultDialog(
			parent=nvda_gui.mainFrame,
			file_path=file_path,
			page_count=page_count,
			provider=provider,
			model=model,
			skill_prompt=pdf_skill.render_prompt(),
			temperature=self._resolve_temperature(pdf_skill),
			timeout=luma_config.get_setting("timeout", 60),
		)
		dlg.Show()
		dlg.Raise()
		# Auto-process page 1.
		wx.CallAfter(dlg.navigate_to_page, 0)

	def _open_pptx_dialog(self, file_path, pdf_path, page_count):
		"""Open the paginated slides dialog and auto-process slide 1.

		*pdf_path* is the temporary PDF produced by the PPTX converter.
		It is cleaned up automatically when the dialog is closed.
		"""
		from . import config as luma_config
		from .dialogs import PdfResultDialog
		from .file_handler.pdf import render_page
		from .file_handler.pptx import cleanup_temp
		from .skill import load_skills

		# Find the builtin Slides Reader skill.
		slides_skill = None
		for sk in load_skills():
			if sk.name == "Slides Reader":
				slides_skill = sk
				break
		if slides_skill is None:
			# Translators: Spoken when the Slides Reader skill is missing.
			ui.message(_("Slides Reader skill not found."))
			cleanup_temp(pdf_path)
			return

		provider, model = self._resolve_provider_and_model(slides_skill)
		if provider is None:
			cleanup_temp(pdf_path)
			return

		# Render slides from the temporary PDF.
		def render_fn(_file_path, page):
			return render_page(pdf_path, page)

		dlg = PdfResultDialog(
			parent=nvda_gui.mainFrame,
			file_path=file_path,
			page_count=page_count,
			provider=provider,
			model=model,
			skill_prompt=slides_skill.render_prompt(),
			temperature=self._resolve_temperature(slides_skill),
			timeout=luma_config.get_setting("timeout", 60),
			render_fn=render_fn,
			cleanup_fn=lambda: cleanup_temp(pdf_path),
		)
		dlg.Show()
		dlg.Raise()
		# Auto-process slide 1.
		wx.CallAfter(dlg.navigate_to_page, 0)

	# -- Screen --

	# Translators: Description for the screen capture gesture.
	@scriptHandler.script(
		description=_("Process entire screen with active skill. Press twice quickly to open text chat."),
	)
	def script_processScreen(self, gesture):
		self._screen_press.tap()

	def _on_screen_tap(self, count):
		if count >= 2:
			from . import capture
			image = capture.capture_screen()
			self._open_text_chat(image_base64=image)
		else:
			skill = self._require_active_skill()
			if skill:
				self._execute_skill_with_scope(skill, "screen")

	# -- Application --

	# Translators: Description for the current application gesture.
	@scriptHandler.script(
		description=_("Process current application with active skill. Press twice quickly to open text chat."),
	)
	def script_processApp(self, gesture):
		self._app_press.tap()

	def _on_app_tap(self, count):
		if count >= 2:
			from . import capture
			image = capture.capture_app()
			self._open_text_chat(image_base64=image)
		else:
			skill = self._require_active_skill()
			if skill:
				self._execute_skill_with_scope(skill, "app")

	# -- Clipboard --

	# Translators: Description for the clipboard processing gesture.
	@scriptHandler.script(
		description=_("Process clipboard with active skill. Press twice quickly to open text chat."),
	)
	def script_processClipboard(self, gesture):
		self._clipboard_press.tap()

	def _on_clipboard_tap(self, count):
		from .clipboard import get_clipboard_content

		content_type, data = get_clipboard_content()

		if content_type == "empty":
			# Translators: Spoken when the clipboard is empty.
			ui.message(_("Clipboard is empty."))
			return

		if count >= 2:
			if content_type == "image":
				self._open_text_chat(image_base64=data)
			else:
				self._open_text_chat_with_text(data)
		else:
			skill = self._require_active_skill()
			if skill:
				if content_type == "image":
					# Translators: Source label for clipboard image processing.
					self._execute_skill_with_scope(skill, "navigator", image_base64=data, source_label=_("clipboard image"))
				else:
					self._process_text_with_skill(skill, data)

	def _process_text_with_skill(self, skill, text):
		"""Process text content with the given skill."""
		provider, model = self._resolve_provider_and_model(skill)
		if provider is None:
			return

		prompt = skill.render_prompt()
		full_prompt = f"{prompt}\n\n{text}"

		# Translators: Spoken when clipboard text processing begins.
		# {skill} is the active skill name (e.g. "Describe").
		ui.message(_("Processing clipboard using {skill}...").format(skill=skill.name))

		from .worker import process_skill
		from .result_handler import show_result, show_error

		# Cancel any previously running processing task.
		if self._processing_cancel is not None:
			self._processing_cancel.set()
		self._processing_cancel = threading.Event()

		from . import config as luma_config

		def _on_success(result):
			self._stash_last_exchange(
				skill=skill,
				image_base64=None,
				prompt=full_prompt,
				response=result,
			)
			show_result(result, title=skill.name, skill_name=skill.name)

		process_skill(
			provider,
			model=model,
			prompt=full_prompt,
			image_base64=None,
			temperature=self._resolve_temperature(skill),
			timeout=luma_config.get_setting("timeout", 60),
			on_success=_on_success,
			on_error=show_error,
			cancel_event=self._processing_cancel,
		)

	def _open_text_chat_with_text(self, text):
		"""Open text chat with clipboard text as initial context."""
		from .dialogs import TextChatDialog

		# Use active skill for provider/model, but don't inject its
		# one-shot prompt into the chat (same logic as _open_text_chat).
		resolve_skill = self._get_active_skill()
		chat_skill = None  # only set if a chat-appropriate skill is found
		if resolve_skill is None:
			from .skill import load_skills
			for sk in load_skills():
				if sk.name.lower() == "text chat":
					resolve_skill = sk
					chat_skill = sk
					break

		provider, model = self._resolve_provider_and_model(resolve_skill)
		if provider is None:
			return

		dlg = TextChatDialog(
			parent=nvda_gui.mainFrame,
			provider=provider,
			model=model,
			skill=chat_skill,
			clipboard_text=text,
			temperature=self._resolve_temperature(resolve_skill),
		)
		dlg.Show()
		dlg.Raise()

	# -- Camera --

	# Translators: Description for the camera capture gesture.
	@scriptHandler.script(
		description=_("Process camera image with active skill. Press twice quickly to open text chat."),
	)
	def script_processCamera(self, gesture):
		self._camera_press.tap()

	def _on_camera_tap(self, count):
		if count >= 2:
			self._capture_and_process_camera(skill=None, open_chat=True)
		else:
			skill = self._require_active_skill()
			if skill:
				self._capture_and_process_camera(skill, open_chat=False)

	def _capture_and_process_camera(self, skill, *, open_chat=False):
		"""Capture a camera frame in the background and process it.

		If *open_chat* is True, opens text chat with the image.
		Otherwise processes with *skill*.
		"""
		from .camera import is_camera_available

		if not is_camera_available():
			# Translators: Spoken when no camera is detected.
			ui.message(_("No camera found."))
			return

		# Translators: Spoken when camera capture begins.
		ui.message(_("Capturing from camera..."))

		from .camera import capture_camera
		from .worker import run_in_background

		# Cancel any previously running processing task.
		if self._processing_cancel is not None:
			self._processing_cancel.set()
		self._processing_cancel = threading.Event()

		def on_success(image_base64):
			if image_base64 is None:
				# Translators: Spoken when camera capture fails.
				ui.message(_("Failed to capture from camera."))
				return
			if open_chat:
				self._open_text_chat(image_base64=image_base64)
			else:
				# Translators: Source label for camera image processing.
				self._execute_skill_with_scope(
					skill, "navigator",
					image_base64=image_base64,
					source_label=_("camera image"),
				)

		def on_error(exc):
			log.exception("Camera capture failed")
			# Translators: Spoken when camera capture fails.
			ui.message(_("Failed to capture from camera."))

		run_in_background(
			capture_camera,
			on_success=on_success,
			on_error=on_error,
			cancel_event=self._processing_cancel,
		)

	# ==================================================================
	# AI File Rename
	# ==================================================================

	# Translators: Description for the AI file rename gesture.
	@scriptHandler.script(
		description=_("Rename selected image files in File Explorer using AI"),
	)
	def script_renameFiles(self, gesture):
		wx.CallAfter(self._rename_selected_files)

	def _rename_selected_files(self):
		"""Rename selected image files in File Explorer using AI."""
		from . import file_handler
		from . import config as luma_config
		from .skill import load_skills

		# Find the builtin File Renamer skill.
		rename_skill = None
		for sk in load_skills():
			if sk.name == "File Renamer":
				rename_skill = sk
				break
		if rename_skill is None:
			# Translators: Spoken when the File Renamer skill is missing.
			ui.message(_("File Renamer skill not found."))
			return

		provider, model = self._resolve_provider_and_model(rename_skill)
		if provider is None:
			return

		# Get all selected files from Explorer.
		files = file_handler.get_explorer_selected_files()
		if not files:
			# Translators: Spoken when no file is selected in File Explorer.
			ui.message(_("No file selected in File Explorer."))
			return

		# Partition into image files and skipped files.
		image_files = []
		skipped = 0
		for f in files:
			ext = os.path.splitext(f)[1].lower()
			if ext in file_handler.IMAGE_EXTENSIONS:
				image_files.append(f)
			else:
				skipped += 1

		if not image_files:
			# Translators: Spoken when no image files are among the selection.
			ui.message(_("No image files selected. Only JPEG and PNG files can be renamed."))
			return

		if skipped > 0:
			# Translators: Spoken when non-image files are skipped during rename.
			# {count} is the number of skipped files.
			ui.message(_("Skipping {count} non-image file(s).").format(count=skipped))

		# Hard cap to protect API quota.
		_MAX_RENAME_FILES = 50
		if len(image_files) > _MAX_RENAME_FILES:
			# Translators: Spoken when more than the maximum image files are selected.
			# {max} is the limit, {count} is the total number of image files found.
			ui.message(
				_("Processing the first {max} of {count} image files.").format(
					max=_MAX_RENAME_FILES, count=len(image_files),
				)
			)
			image_files = image_files[:_MAX_RENAME_FILES]

		# Translators: Spoken when AI file renaming begins.
		# {count} is the number of files being renamed.
		ui.message(
			_("Renaming {count} file(s) using AI...").format(count=len(image_files))
		)

		prompt = rename_skill.render_prompt()
		temperature = self._resolve_temperature(rename_skill)
		timeout = luma_config.get_setting("timeout", 60)

		from .worker import run_in_background

		def _process_all():
			import datetime
			renamed = 0
			for fp in image_files:
				basename = os.path.basename(fp)
				try:
					image_b64 = file_handler.read_image_file(fp)
					if image_b64 is None:
						wx.CallAfter(
							ui.message,
							_("Could not read {filename}.").format(filename=basename),
						)
						continue
					raw_name = provider.process_request(
						model=model,
						messages=[{"role": "user", "content": prompt}],
						temperature=temperature,
						image_base64=image_b64,
						timeout=timeout,
					)
					new_stem = file_handler.sanitize_filename(raw_name)
					# Append the file's modification date (usually the capture date).
					mtime = os.path.getmtime(fp)
					date_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
					new_stem = f"{new_stem}_{date_str}"
					ext = os.path.splitext(fp)[1]
					directory = os.path.dirname(fp)
					new_path = os.path.join(directory, new_stem + ext)
					# Avoid overwriting existing files.
					counter = 1
					while os.path.exists(new_path) and new_path != fp:
						new_path = os.path.join(
							directory, f"{new_stem}-{counter}{ext}",
						)
						counter += 1
					os.rename(fp, new_path)
					new_name = os.path.basename(new_path)
					renamed += 1
					# Translators: Spoken when a file has been successfully renamed.
					# {old} is the original filename, {new} is the new filename.
					wx.CallAfter(
						ui.message,
						_("Renamed {old} to {new}").format(old=basename, new=new_name),
					)
				except Exception as exc:
					log.exception("Failed to rename %s", fp)
					# Translators: Spoken when a file rename fails.
					# {filename} is the original filename, {error} is the error detail.
					wx.CallAfter(
						ui.message,
						_("Could not rename {filename}: {error}").format(
							filename=basename, error=str(exc),
						),
					)
			return renamed

		def _on_done(renamed_count):
			# Translators: Spoken when all rename operations are finished.
			# {count} is the number of successfully renamed files.
			ui.message(
				_("Finished. {count} file(s) renamed.").format(count=renamed_count)
			)

		run_in_background(
			_process_all,
			on_success=_on_done,
			on_error=lambda exc: ui.message(
				_("Rename operation failed: {error}").format(error=str(exc)),
			),
		)

	# ==================================================================
	# Video analysis
	# ==================================================================

	# Translators: Description for the video analysis gesture.
	@scriptHandler.script(
		description=_(
			"Analyze a video from YouTube, Instagram, Twitter, or TikTok"
		),
	)
	def script_processVideo(self, gesture):
		wx.CallAfter(self._process_video)

	def _process_video(self):
		"""Analyze a video URL using AI."""
		from . import video
		from . import config as luma_config
		from .skill import load_skills

		# 1. Try to auto-detect the URL from the browser.
		url = video.detect_browser_url()

		# 2. If auto-detection found a URL, validate that it's a
		#    supported video platform.  If not, fall through to the
		#    dialog so the user can paste the correct URL.
		if url:
			try:
				video.parse_video_url(url)
			except ValueError:
				url = None

		# 3. Fallback: show a URL entry dialog.
		if not url:
			# Translators: Title for the video URL entry dialog.
			title = _("Video Analysis")
			# Translators: Prompt in the video URL entry dialog.
			msg = _(
				"Enter a video URL "
				"(YouTube, Instagram, Twitter, or TikTok):"
			)
			dlg = wx.TextEntryDialog(nvda_gui.mainFrame, msg, title)
			dlg.Raise()
			if dlg.ShowModal() != wx.ID_OK:
				dlg.Destroy()
				return
			url = dlg.GetValue().strip()
			dlg.Destroy()
			if not url:
				return

		# 4. Parse and validate.
		try:
			platform, normalized_url = video.parse_video_url(url)
		except ValueError as exc:
			ui.message(str(exc))
			return

		# 5. Find the Video Description skill.
		video_skill = None
		for sk in load_skills():
			if sk.name == "Video Description":
				video_skill = sk
				break
		if video_skill is None:
			# Translators: Spoken when the Video Description skill is missing.
			ui.message(_("Video Description skill not found."))
			return

		# 6. Resolve provider and model.
		provider, model = self._resolve_provider_and_model(video_skill)
		if provider is None:
			return

		# 7. Check provider supports video.
		if not provider.supports_video:
			# Translators: Spoken when the provider does not support video.
			# {provider} is the provider name.
			ui.message(
				_(
					"{provider} does not support video analysis. "
					"Please configure a Gemini provider."
				).format(provider=provider.name)
			)
			return

		# 8. Speak status and launch background processing.
		# Translators: Spoken when video processing begins.
		ui.message(_("Processing video..."))

		prompt = video_skill.render_prompt()
		temperature = self._resolve_temperature(video_skill)

		from .worker import run_in_background
		from . import result_handler

		def _process():
			video_uri = normalized_url

			# For non-YouTube platforms, extract the direct link,
			# download the video, and upload it to the provider.
			temp_path = None
			if platform != video.PLATFORM_YOUTUBE:
				# Translators: Spoken when extracting a video link.
				wx.CallAfter(ui.message, _("Extracting video link..."))
				direct_link = video.get_direct_video_link(
					normalized_url, platform,
				)
				if not direct_link:
					# Translators: Spoken when the video link could not be
					# extracted from the social media platform.
					raise ConnectionError(
						_("Could not extract the video link from this URL.")
					)
				# Translators: Spoken when downloading a video.
				wx.CallAfter(ui.message, _("Downloading video..."))
				temp_path = video.download_video(direct_link)
				try:
					# Translators: Spoken when uploading a video to the AI.
					wx.CallAfter(ui.message, _("Uploading video to AI..."))
					video_uri = provider.upload_video(temp_path)
				except Exception:
					# Clean up temp file on upload failure.
					try:
						os.remove(temp_path)
					except OSError:
						pass
					raise

			try:
				# Translators: Spoken when the AI is analyzing the video.
				wx.CallAfter(ui.message, _("Analyzing..."))
				return provider.process_video_request(
					model=model,
					messages=[{"role": "user", "content": prompt}],
					temperature=temperature,
					video_uri=video_uri,
					timeout=300,
				)
			finally:
				# Clean up temp file if we downloaded one.
				if temp_path:
					try:
						os.remove(temp_path)
					except OSError:
						pass

		def _on_success(text):
			# Video follow-up degrades to a pure text chat: we don't have
			# a single image to re-attach, and TextChatDialog uses the
			# non-video process_request path. Stashing the prompt + response
			# still lets the user ask clarifying questions about the text
			# of the analysis.
			self._stash_last_exchange(
				skill=video_skill,
				image_base64=None,
				prompt=prompt,
				response=text,
			)
			result_handler.show_result(
				text, title=video_skill.name, skill_name=video_skill.name,
			)

		run_in_background(
			_process,
			on_success=_on_success,
			on_error=lambda exc: result_handler.show_error(exc),
		)

	# ==================================================================
	# Text chat
	# ==================================================================

	# Translators: Description for the text chat gesture.
	@scriptHandler.script(description=_("Open Luma text chat"))
	def script_textChat(self, gesture):
		self._open_text_chat()

	# ==================================================================
	# Interaction Mode
	# ==================================================================

	# Translators: Description for the interaction mode gesture.
	@scriptHandler.script(
		description=_("Open interaction mode (AI computer control)"),
	)
	def script_interactionMode(self, gesture):
		wx.CallAfter(self._open_interaction_mode)

	def _resolve_interaction_provider_and_model(self):
		"""Resolve the provider and model for Interaction Mode.

		Checks the dedicated interaction_provider/interaction_model settings
		first.  If not set (auto-detect), searches configured providers for
		one that supports interaction.

		Returns ``(provider, model)`` or ``(None, None)`` with a spoken
		error message.
		"""
		from . import config as luma_config
		from .providers import provider_from_dict

		settings = luma_config.get("settings") or {}
		interaction_provider_name = settings.get("interaction_provider", "")
		interaction_model = settings.get("interaction_model", "")

		providers = luma_config.get_configured_providers()
		if not providers:
			# Translators: Spoken when no providers are configured.
			ui.message(_("No providers configured. Please add a provider first."))
			return None, None

		# If the user explicitly configured an interaction provider, use it.
		if interaction_provider_name:
			pdata = None
			for p in providers:
				if p.get("name") == interaction_provider_name:
					pdata = p
					break
			if pdata is None:
				# Translators: Spoken when the configured interaction provider is not found.
				ui.message(
					_("Interaction provider \"{name}\" not found. Please check Settings.").format(
						name=interaction_provider_name,
					)
				)
				return None, None

			try:
				provider = provider_from_dict(pdata)
			except Exception as exc:
				log.exception("Failed to create interaction provider")
				ui.message(_("Provider error: {error}").format(error=str(exc)))
				return None, None

			if not provider.supports_interaction:
				# Translators: Spoken when the configured interaction provider
				# doesn't support computer use.
				ui.message(
					_(
						"The provider \"{name}\" does not support Interaction Mode. "
						"Please select Anthropic or Google Gemini in Settings."
					).format(name=interaction_provider_name)
				)
				return None, None

			if not interaction_model:
				# Translators: Spoken when no interaction model is set.
				ui.message(
					_("No interaction model configured. Please set one in Settings.")
				)
				return None, None

			return provider, interaction_model

		# Auto-detect: find the first configured provider that supports interaction.
		for pdata in providers:
			try:
				provider = provider_from_dict(pdata)
			except Exception:
				continue
			if provider.supports_interaction:
				# Use a sensible default model for each provider type.
				model = interaction_model or self._default_interaction_model(provider)
				if model:
					return provider, model

		# Translators: Spoken when no interaction-capable provider is found.
		ui.message(
			_(
				"No provider supporting Interaction Mode was found. "
				"Please configure Anthropic or Google Gemini with an API key."
			)
		)
		return None, None

	@staticmethod
	def _default_interaction_model(provider) -> str:
		"""Return a sensible default model for interaction mode."""
		from .providers.anthropic import AnthropicProvider
		from .providers.gemini import GeminiProvider

		if isinstance(provider, AnthropicProvider):
			return "claude-sonnet-4-20250514"
		if isinstance(provider, GeminiProvider):
			return "gemini-2.0-flash"
		return ""

	def _open_interaction_mode(self):
		"""Open the Interaction Mode dialog."""
		from .interaction import InteractionDialog

		provider, model = self._resolve_interaction_provider_and_model()
		if provider is None:
			return

		dlg = InteractionDialog(
			parent=nvda_gui.mainFrame,
			provider=provider,
			model=model,
		)
		self._interaction_dialog = dlg
		dlg.Show()
		dlg.Raise()

	# ==================================================================
	# Live mode
	# ==================================================================

	# Translators: Description for the live mode toggle gesture.
	@scriptHandler.script(
		description=_("Toggle Luma Live mode (proactive continuous narration)"),
	)
	def script_toggleLiveMode(self, gesture):
		wx.CallAfter(self._live_mode.toggle)

	# ==================================================================
	# Stop processing / interaction
	# ==================================================================

	# Translators: Description for the stop processing gesture.
	@scriptHandler.script(
		description=_("Stop the current processing request or interaction"),
	)
	def script_stopProcessing(self, gesture):
		cancelled = False

		# Cancel active processing task.
		if self._processing_cancel is not None:
			self._processing_cancel.set()
			self._processing_cancel = None
			cancelled = True

		# Cancel active interaction mode.
		try:
			if (
				self._interaction_dialog is not None
				and self._interaction_dialog._running
			):
				self._interaction_dialog._on_cancel()
				cancelled = True
		except RuntimeError:
			# Dialog was already destroyed.
			self._interaction_dialog = None

		if cancelled:
			from .worker import force_stop_sound

			force_stop_sound()
			# Translators: Spoken when the user cancels processing.
			ui.message(_("Cancelled."))
		else:
			# Translators: Spoken when there is nothing to cancel.
			ui.message(_("Nothing to cancel."))

	# ==================================================================
	# Toggle active skill
	# ==================================================================

	# Translators: Description for the toggle active skill gesture.
	@scriptHandler.script(description=_("Toggle active skill"))
	def script_toggleActiveSkill(self, gesture):
		from . import config as luma_config
		from .skill import load_skills

		skills = load_skills()
		if not skills:
			# Translators: Spoken when no skills are available.
			ui.message(_("No skills available."))
			return

		active_name = luma_config.get("active_skill")
		skill_names = [sk.name for sk in skills]

		try:
			current_idx = skill_names.index(active_name)
			next_idx = (current_idx + 1) % len(skill_names)
		except ValueError:
			next_idx = 0

		new_name = skill_names[next_idx]
		luma_config.set("active_skill", new_name)
		luma_config.save()
		# Translators: Spoken when the active skill is changed. {name} is the skill name.
		ui.message(_("Active skill: {name}").format(name=new_name))

	# ==================================================================
	# Custom shortcuts
	# ==================================================================

	# Translators: Description for the custom shortcut 1 gesture.
	@scriptHandler.script(description=_("Execute custom shortcut 1"))
	def script_shortcut1(self, gesture):
		self._run_shortcut("1")

	# Translators: Description for the custom shortcut 2 gesture.
	@scriptHandler.script(description=_("Execute custom shortcut 2"))
	def script_shortcut2(self, gesture):
		self._run_shortcut("2")

	# Translators: Description for the custom shortcut 3 gesture.
	@scriptHandler.script(description=_("Execute custom shortcut 3"))
	def script_shortcut3(self, gesture):
		self._run_shortcut("3")

	# Translators: Description for the custom shortcut 4 gesture.
	@scriptHandler.script(description=_("Execute custom shortcut 4"))
	def script_shortcut4(self, gesture):
		self._run_shortcut("4")

	# Translators: Description for the custom shortcut 5 gesture.
	@scriptHandler.script(description=_("Execute custom shortcut 5"))
	def script_shortcut5(self, gesture):
		self._run_shortcut("5")

	# Translators: Description for the custom shortcut 6 gesture.
	@scriptHandler.script(description=_("Execute custom shortcut 6"))
	def script_shortcut6(self, gesture):
		self._run_shortcut("6")

	# Translators: Description for the custom shortcut 7 gesture.
	@scriptHandler.script(description=_("Execute custom shortcut 7"))
	def script_shortcut7(self, gesture):
		self._run_shortcut("7")

	# Translators: Description for the custom shortcut 8 gesture.
	@scriptHandler.script(description=_("Execute custom shortcut 8"))
	def script_shortcut8(self, gesture):
		self._run_shortcut("8")

	# Translators: Description for the custom shortcut 9 gesture.
	@scriptHandler.script(description=_("Execute custom shortcut 9"))
	def script_shortcut9(self, gesture):
		self._run_shortcut("9")

	# Translators: Description for the custom shortcut 0 gesture.
	@scriptHandler.script(description=_("Execute custom shortcut 10"))
	def script_shortcut0(self, gesture):
		self._run_shortcut("0")

	def _run_shortcut(self, key: str):
		"""Execute the shortcut mapped to digit *key*."""
		from . import config as luma_config
		from .skill import load_skills

		shortcuts = luma_config.get("shortcuts") or {}
		mapping = shortcuts.get(key)
		if not mapping:
			# Translators: Spoken when no shortcut is assigned. {key} is the digit.
			ui.message(_("No shortcut assigned to NVDA+Shift+{key}.").format(key=key))
			return

		skill_name = mapping.get("skill", "")
		scope = mapping.get("scope", "")

		if not skill_name or not scope:
			# Translators: Spoken when a shortcut is incomplete.
			ui.message(_("Shortcut {key} is not fully configured.").format(key=key))
			return

		skill = None
		for sk in load_skills():
			if sk.name == skill_name:
				skill = sk
				break

		if skill is None:
			# Translators: Spoken when a shortcut references a missing skill. {name} is the name.
			ui.message(_("Skill \"{name}\" not found.").format(name=skill_name))
			return

		self._execute_skill_with_scope(skill, scope)

	# ==================================================================
	# Gesture bindings — from the canonical GESTURE_MAP.
	# ==================================================================

	__gestures = GESTURE_MAP
