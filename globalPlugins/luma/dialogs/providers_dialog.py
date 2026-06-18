"""Provider management dialogs for Luma.

Provides dialogs for configuring preset providers (API key entry)
and custom OpenAI-compatible providers (add/edit/delete).
"""

from __future__ import annotations

import logging

import wx

import ui

from .. import config as luma_config
from ..providers import provider_from_dict

log = logging.getLogger(__name__)


class ProvidersDialog(wx.Dialog):
	"""Configure preset providers (API key only) and custom OpenAI-compatible providers."""

	def __init__(self, parent: wx.Window | None = None) -> None:
		# Translators: Title of the providers management dialog.
		super().__init__(parent, title=_("Configure Providers"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		from ..providers.presets import PRESETS
		self._presets = PRESETS
		self._build_ui()
		self._refresh_presets()
		self._refresh_custom()
		self.CentreOnScreen()

	def _build_ui(self) -> None:
		panel = wx.Panel(self)
		sizer = wx.BoxSizer(wx.VERTICAL)

		# -- Preset providers section --
		# Translators: Label above the preset providers list.
		sizer.Add(wx.StaticText(panel, label=_("&Preset Providers:")), flag=wx.LEFT | wx.TOP, border=10)

		self._preset_list = wx.ListBox(panel)
		self._preset_list.Bind(wx.EVT_LISTBOX, self._on_preset_selection_changed)
		sizer.Add(self._preset_list, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		preset_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Button to set or change the API key for a preset provider.
		self._btn_set_key = wx.Button(panel, label=_("Set API &Key..."))
		self._btn_set_key.Bind(wx.EVT_BUTTON, self._on_set_key)
		preset_btn_sizer.Add(self._btn_set_key, flag=wx.RIGHT, border=8)

		# Translators: Button to remove the API key from a preset provider.
		self._btn_remove_key = wx.Button(panel, label=_("&Remove API Key"))
		self._btn_remove_key.Bind(wx.EVT_BUTTON, self._on_remove_key)
		preset_btn_sizer.Add(self._btn_remove_key)

		sizer.Add(preset_btn_sizer, flag=wx.LEFT | wx.TOP | wx.BOTTOM, border=10)

		# -- Custom providers section --
		# Translators: Label above the custom providers list.
		sizer.Add(wx.StaticText(panel, label=_("C&ustom Providers (OpenAI Compatible):")), flag=wx.LEFT | wx.TOP, border=10)

		self._custom_list = wx.ListBox(panel)
		sizer.Add(self._custom_list, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		custom_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Button to add a new custom provider.
		self._btn_new = wx.Button(panel, label=_("&New Provider..."))
		self._btn_new.Bind(wx.EVT_BUTTON, self._on_new)
		custom_btn_sizer.Add(self._btn_new, flag=wx.RIGHT, border=8)

		# Translators: Button to edit the selected custom provider.
		self._btn_edit = wx.Button(panel, label=_("&Edit Provider..."))
		self._btn_edit.Bind(wx.EVT_BUTTON, self._on_edit)
		custom_btn_sizer.Add(self._btn_edit, flag=wx.RIGHT, border=8)

		# Translators: Button to delete the selected custom provider.
		self._btn_delete = wx.Button(panel, label=_("&Delete Provider"))
		self._btn_delete.Bind(wx.EVT_BUTTON, self._on_delete)
		custom_btn_sizer.Add(self._btn_delete)

		sizer.Add(custom_btn_sizer, flag=wx.LEFT | wx.TOP | wx.BOTTOM, border=10)

		# -- Close --
		# Translators: Button to close the dialog.
		close_btn = wx.Button(panel, id=wx.ID_CLOSE, label=_("Close"))
		close_btn.Bind(wx.EVT_BUTTON, lambda _e: self.Destroy())
		self.SetEscapeId(wx.ID_CLOSE)
		sizer.Add(close_btn, flag=wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, border=10)

		panel.SetSizer(sizer)
		self.SetSize((520, 520))
		self.SetMinSize((440, 400))

	# -- Preset providers ----------------------------------------------------

	def _refresh_presets(self) -> None:
		self._preset_list.Clear()
		api_keys = luma_config.get_preset_api_keys()
		for preset in self._presets:
			has_key = bool(api_keys.get(preset.id))
			if preset.id == "local":
				# Show setup status for the Local preset.
				from ..local_inference import get_status
				label = f"{preset.name} — {get_status()}"
			elif has_key:
				if preset.requires_key:
					# Translators: Suffix for a preset provider that has an API key configured.
					label = _("{name} (configured)").format(name=preset.name)
				else:
					# Translators: Suffix for a local provider that has been enabled.
					label = _("{name} (enabled)").format(name=preset.name)
			else:
				label = preset.name
			self._preset_list.Append(label)
		if self._preset_list.GetCount() > 0:
			self._preset_list.SetSelection(0)
		self._update_preset_buttons()

	def _on_preset_selection_changed(self, _event: wx.CommandEvent) -> None:
		self._update_preset_buttons()

	def _update_preset_buttons(self) -> None:
		"""Update button labels based on whether the selected preset needs an API key."""
		idx = self._preset_list.GetSelection()
		if idx == wx.NOT_FOUND:
			return
		preset = self._presets[idx]
		if preset.id == "local":
			# Translators: Button to open the local inference setup wizard.
			self._btn_set_key.SetLabel(_("&Set Up..."))
			# Translators: Button to disable local inference.
			self._btn_remove_key.SetLabel(_("&Disable"))
		elif preset.requires_key:
			# Translators: Button to set or change the API key for a preset provider.
			self._btn_set_key.SetLabel(_("Set API &Key..."))
			# Translators: Button to remove the API key from a preset provider.
			self._btn_remove_key.SetLabel(_("&Remove API Key"))
		else:
			# Translators: Button to enable a local provider (e.g. Ollama).
			self._btn_set_key.SetLabel(_("&Enable"))
			# Translators: Button to disable a local provider (e.g. Ollama).
			self._btn_remove_key.SetLabel(_("&Disable"))

	def _on_set_key(self, _event: wx.CommandEvent) -> None:
		idx = self._preset_list.GetSelection()
		if idx == wx.NOT_FOUND:
			return
		preset = self._presets[idx]
		if preset.id == "local":
			# Launch the local inference setup wizard.
			from ..local_inference.setup_dialog import SetupDialog
			dlg = SetupDialog(self)
			dlg.ShowModal()
			dlg.Destroy()
			self._refresh_presets()
			return
		if not preset.requires_key:
			# Keyless provider — enable directly without a dialog.
			luma_config.set_preset_api_key(preset.id, "enabled")
			self._refresh_presets()
			return
		current_key = luma_config.get_preset_api_key(preset.id)
		dlg = _ApiKeyDialog(self, preset_name=preset.name, current_key=current_key)
		if dlg.ShowModal() == wx.ID_OK:
			luma_config.set_preset_api_key(preset.id, dlg.get_api_key())
			self._refresh_presets()
		dlg.Destroy()

	def _on_remove_key(self, _event: wx.CommandEvent) -> None:
		idx = self._preset_list.GetSelection()
		if idx == wx.NOT_FOUND:
			return
		preset = self._presets[idx]
		if not luma_config.get_preset_api_key(preset.id):
			return
		if not preset.requires_key:
			# Translators: Confirmation when disabling a local provider. {name} is the provider name.
			msg = _("Disable \"{name}\"?").format(name=preset.name)
			# Translators: Title of the disable-provider confirmation dialog.
			title = _("Disable Provider")
		else:
			# Translators: Confirmation when removing a preset API key. {name} is the provider name.
			msg = _("Remove the API key for \"{name}\"?").format(name=preset.name)
			# Translators: Title of the remove-API-key confirmation dialog.
			title = _("Remove API Key")
		if wx.MessageBox(msg, title, wx.YES_NO | wx.ICON_WARNING, self) == wx.YES:
			luma_config.set_preset_api_key(preset.id, "")
			self._refresh_presets()

	# -- Custom providers ----------------------------------------------------

	def _refresh_custom(self) -> None:
		self._custom_list.Clear()
		for p in luma_config.get_custom_providers():
			self._custom_list.Append(p.get("name", _("Unnamed")))
		if self._custom_list.GetCount() > 0:
			self._custom_list.SetSelection(0)

	def _on_new(self, _event: wx.CommandEvent) -> None:
		dlg = CustomProviderEditDialog(self)
		if dlg.ShowModal() == wx.ID_OK:
			data = dlg.get_provider_dict()
			luma_config.add_custom_provider(data)
			self._refresh_custom()
		dlg.Destroy()

	def _on_edit(self, _event: wx.CommandEvent) -> None:
		idx = self._custom_list.GetSelection()
		if idx == wx.NOT_FOUND:
			return
		providers = luma_config.get_custom_providers()
		dlg = CustomProviderEditDialog(self, provider_data=providers[idx])
		if dlg.ShowModal() == wx.ID_OK:
			data = dlg.get_provider_dict()
			luma_config.update_custom_provider(idx, data)
			self._refresh_custom()
		dlg.Destroy()

	def _on_delete(self, _event: wx.CommandEvent) -> None:
		idx = self._custom_list.GetSelection()
		if idx == wx.NOT_FOUND:
			return
		name = self._custom_list.GetString(idx)
		# Translators: Confirmation prompt when deleting a custom provider. {name} is the name.
		if wx.MessageBox(
			_("Are you sure you want to delete the provider \"{name}\"?").format(name=name),
			# Translators: Title of the delete-provider confirmation dialog.
			_("Delete Provider"),
			wx.YES_NO | wx.ICON_WARNING,
			self,
		) == wx.YES:
			luma_config.remove_custom_provider(idx)
			self._refresh_custom()


class _ApiKeyDialog(wx.Dialog):
	"""Simple dialog to enter an API key for a preset provider."""

	def __init__(
		self,
		parent: wx.Window,
		preset_name: str,
		current_key: str = "",
	) -> None:
		# Translators: Title of the API key dialog. {name} is the provider name.
		title = _("API Key — {name}").format(name=preset_name)
		super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self._build_ui(current_key)
		self.CentreOnScreen()

	def _build_ui(self, current_key: str) -> None:
		sizer = wx.BoxSizer(wx.VERTICAL)

		# Translators: Label for the API key field.
		sizer.Add(wx.StaticText(self, label=_("API &Key:")), flag=wx.LEFT | wx.TOP, border=10)
		self._api_key = wx.TextCtrl(self, value=current_key, style=wx.TE_PASSWORD)
		sizer.Add(self._api_key, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
		sizer.Add(btn_sizer, flag=wx.EXPAND | wx.ALL, border=10)

		self.SetSizer(sizer)
		self.SetSize((420, 160))
		self.SetMinSize((350, 140))
		self._api_key.SetFocus()

	def get_api_key(self) -> str:
		return self._api_key.GetValue().strip()


class CustomProviderEditDialog(wx.Dialog):
	"""Add or edit a custom OpenAI-compatible provider."""

	def __init__(
		self,
		parent: wx.Window,
		provider_data: dict | None = None,
	) -> None:
		if provider_data:
			# Translators: Title when editing an existing provider.
			title = _("Edit Provider")
		else:
			# Translators: Title when adding a new provider.
			title = _("New Provider")
		super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self._data = provider_data or {}
		self._build_ui()
		self.CentreOnScreen()

	def _build_ui(self) -> None:
		sizer = wx.BoxSizer(wx.VERTICAL)

		# Translators: Label for the provider name field.
		sizer.Add(wx.StaticText(self, label=_("&Name:")), flag=wx.LEFT | wx.TOP, border=10)
		self._name = wx.TextCtrl(self, value=self._data.get("name", ""))
		sizer.Add(self._name, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# Translators: Label for the API URL field.
		sizer.Add(wx.StaticText(self, label=_("API &URL:")), flag=wx.LEFT | wx.TOP, border=10)
		self._api_url = wx.TextCtrl(self, value=self._data.get("api_url", ""))
		sizer.Add(self._api_url, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# Translators: Label for the API key field.
		sizer.Add(wx.StaticText(self, label=_("API &Key:")), flag=wx.LEFT | wx.TOP, border=10)
		self._api_key = wx.TextCtrl(self, value=self._data.get("api_key", ""), style=wx.TE_PASSWORD)
		sizer.Add(self._api_key, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# Translators: Label for the model fetch URL field.
		sizer.Add(wx.StaticText(self, label=_("Model &Fetch URL (optional):")), flag=wx.LEFT | wx.TOP, border=10)
		self._model_fetch_url = wx.TextCtrl(self, value=self._data.get("model_fetch_url", ""))
		sizer.Add(self._model_fetch_url, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
		sizer.Add(btn_sizer, flag=wx.EXPAND | wx.ALL, border=10)

		self.SetSizer(sizer)
		self.SetSize((500, 320))
		self.SetMinSize((400, 280))
		self._name.SetFocus()

	def get_provider_dict(self) -> dict:
		return {
			"type": "OpenAICompatProvider",
			"name": self._name.GetValue().strip(),
			"api_url": self._api_url.GetValue().strip(),
			"api_key": self._api_key.GetValue().strip(),
			"model_fetch_url": self._model_fetch_url.GetValue().strip(),
		}
