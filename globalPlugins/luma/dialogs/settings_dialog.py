"""Global settings dialog for Luma."""

from __future__ import annotations

import logging

import wx

import ui

from .. import config as luma_config
from ..providers import provider_from_dict
from ..worker import run_in_background

log = logging.getLogger(__name__)


class SettingsDialog(wx.Dialog):
	"""Global Luma settings: default provider/model, output preference."""

	def __init__(self, parent: wx.Window | None = None) -> None:
		# Translators: Title of the Luma settings dialog.
		super().__init__(parent, title=_("Luma Settings"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self._providers_data = luma_config.get_configured_providers()
		self._fetched_models: list[str] = []
		self._build_ui()
		self._load_values()
		self.CentreOnScreen()

	def _build_ui(self) -> None:
		sizer = wx.BoxSizer(wx.VERTICAL)

		# -- Default provider --
		# Translators: Label for the default provider combo box.
		sizer.Add(wx.StaticText(self, label=_("Default &Provider:")), flag=wx.LEFT | wx.TOP, border=10)
		provider_names = [p.get("name", _("Unnamed")) for p in self._providers_data]
		self._provider_choice = wx.Choice(self, choices=provider_names)
		self._provider_choice.Bind(wx.EVT_CHOICE, self._on_provider_changed)
		sizer.Add(self._provider_choice, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# -- Default model --
		# Translators: Label for the default model combo box.
		sizer.Add(wx.StaticText(self, label=_("Default &Model:")), flag=wx.LEFT | wx.TOP, border=10)
		self._model_choice = wx.ComboBox(self, choices=[], style=wx.CB_DROPDOWN)
		sizer.Add(self._model_choice, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# Translators: Button to fetch available models from the selected provider.
		self._fetch_btn = wx.Button(self, label=_("&Fetch Models"))
		self._fetch_btn.Bind(wx.EVT_BUTTON, self._on_fetch_models)
		sizer.Add(self._fetch_btn, flag=wx.LEFT | wx.TOP, border=10)

		# -- Open in dialog checkbox --
		# Translators: Checkbox label for opening results in a browseable dialog.
		self._dialog_cb = wx.CheckBox(self, label=_("&Open result in a browseable dialog"))
		sizer.Add(self._dialog_cb, flag=wx.LEFT | wx.TOP, border=10)

		# -- Full-size images checkbox --
		# Translators: Checkbox label for sending full-resolution images to providers.
		self._full_images_cb = wx.CheckBox(self, label=_("&Send full-sized images to providers (not needed in most cases!)"))
		sizer.Add(self._full_images_cb, flag=wx.LEFT | wx.TOP, border=10)

		# -- Default temperature --
		# Translators: Label for the default temperature setting.
		sizer.Add(wx.StaticText(self, label=_("Default &Temperature:")), flag=wx.LEFT | wx.TOP, border=10)
		self._temperature = wx.SpinCtrlDouble(self, min=0.0, max=2.0, inc=0.1, initial=0.3)
		self._temperature.SetDigits(1)
		sizer.Add(self._temperature, flag=wx.LEFT | wx.RIGHT, border=10)

		# -- Timeout --
		# Translators: Label for the API timeout setting.
		sizer.Add(wx.StaticText(self, label=_("&Timeout (seconds):")), flag=wx.LEFT | wx.TOP, border=10)
		self._timeout = wx.SpinCtrl(self, min=5, max=300, initial=60)
		sizer.Add(self._timeout, flag=wx.LEFT | wx.RIGHT, border=10)

		sizer.Add(wx.StaticLine(self), flag=wx.EXPAND | wx.TOP | wx.BOTTOM, border=10)

		# -- Interaction mode provider --
		# Translators: Label for the interaction mode provider setting.
		sizer.Add(wx.StaticText(self, label=_("&Interaction Provider:")), flag=wx.LEFT | wx.TOP, border=10)
		interaction_provider_names = [
			# Translators: Option to auto-detect the interaction provider.
			_("(Auto-detect)"),
		] + [p.get("name", _("Unnamed")) for p in self._providers_data]
		self._interaction_provider_choice = wx.Choice(self, choices=interaction_provider_names)
		self._interaction_provider_choice.Bind(wx.EVT_CHOICE, self._on_interaction_provider_changed)
		sizer.Add(self._interaction_provider_choice, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# -- Interaction mode model --
		# Translators: Label for the interaction mode model setting.
		sizer.Add(wx.StaticText(self, label=_("Interaction Mo&del:")), flag=wx.LEFT | wx.TOP, border=10)
		self._interaction_model_choice = wx.ComboBox(self, choices=[], style=wx.CB_DROPDOWN)
		sizer.Add(self._interaction_model_choice, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# Translators: Button to fetch models for interaction mode provider.
		self._interaction_fetch_btn = wx.Button(self, label=_("Fetch &Interaction Models"))
		self._interaction_fetch_btn.Bind(wx.EVT_BUTTON, self._on_fetch_interaction_models)
		sizer.Add(self._interaction_fetch_btn, flag=wx.LEFT | wx.TOP, border=10)

		sizer.Add(wx.StaticLine(self), flag=wx.EXPAND | wx.TOP | wx.BOTTOM, border=10)

		# -- Speech language --
		# Translators: Label for the speech language setting.
		sizer.Add(wx.StaticText(self, label=_("Speech &Language:")), flag=wx.LEFT | wx.TOP, border=10)
		self._speech_language = wx.TextCtrl(self)
		sizer.Add(self._speech_language, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)
		# Translators: Hint text for the speech language setting.
		sizer.Add(
			wx.StaticText(self, label=_("Language for speech input, e.g. 'en', 'de'. Leave empty for NVDA language.")),
			flag=wx.LEFT | wx.BOTTOM, border=10,
		)

		btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
		sizer.Add(btn_sizer, flag=wx.EXPAND | wx.ALL, border=10)
		self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

		self.SetSizer(sizer)
		self.SetSize((500, 650))
		self.SetMinSize((400, 550))

	def _load_values(self) -> None:
		settings = luma_config.get("settings") or {}

		# Select current default provider.
		default_provider = settings.get("default_provider", "")
		for i, p in enumerate(self._providers_data):
			if p.get("name") == default_provider:
				self._provider_choice.SetSelection(i)
				break

		# Set saved model as text in the combobox.
		default_model = settings.get("default_model", "")
		if default_model:
			self._model_choice.SetValue(default_model)

		self._dialog_cb.SetValue(settings.get("open_in_dialog", True))
		self._full_images_cb.SetValue(settings.get("send_full_images", False))
		self._temperature.SetValue(settings.get("default_temperature", 0.3))
		self._timeout.SetValue(settings.get("timeout", 60))

		# Interaction mode provider (index 0 = auto-detect).
		interaction_provider = settings.get("interaction_provider", "")
		if interaction_provider:
			for i, p in enumerate(self._providers_data):
				if p.get("name") == interaction_provider:
					self._interaction_provider_choice.SetSelection(i + 1)  # +1 for auto-detect
					break
			else:
				self._interaction_provider_choice.SetSelection(0)
		else:
			self._interaction_provider_choice.SetSelection(0)

		interaction_model = settings.get("interaction_model", "")
		if interaction_model:
			self._interaction_model_choice.SetValue(interaction_model)

		# Speech language.
		self._speech_language.SetValue(settings.get("speech_language", ""))

	def _on_provider_changed(self, _event: wx.CommandEvent) -> None:
		self._model_choice.Clear()
		self._model_choice.SetValue("")

	def _on_fetch_models(self, _event: wx.CommandEvent) -> None:
		idx = self._provider_choice.GetSelection()
		if idx == wx.NOT_FOUND:
			# Translators: Spoken when no provider is selected for model fetching.
			ui.message(_("Please select a provider first."))
			return
		pdata = self._providers_data[idx]
		provider = provider_from_dict(pdata)

		self._fetch_btn.Enable(False)
		# Translators: Spoken when fetching models from a provider.
		ui.message(_("Fetching models..."))

		def _on_success(models: list[str]) -> None:
			self._fetched_models = models
			self._model_choice.Clear()
			self._model_choice.AppendItems(models)
			self._fetch_btn.Enable(True)
			if models:
				self._model_choice.SetSelection(0)
				# Translators: Spoken when models have been fetched. {count} is the number found.
				ui.message(_("{count} model(s) found.").format(count=len(models)))
			else:
				# Translators: Spoken when no models were found.
				ui.message(_("No models found."))

		def _on_error(exc: Exception) -> None:
			self._fetch_btn.Enable(True)
			# Translators: Spoken when fetching models fails. {error} is the error detail.
			ui.message(_("Failed to fetch models: {error}").format(error=str(exc)))

		run_in_background(provider.fetch_models, on_success=_on_success, on_error=_on_error)

	def _on_interaction_provider_changed(self, _event: wx.CommandEvent) -> None:
		self._interaction_model_choice.Clear()
		self._interaction_model_choice.SetValue("")

	def _on_fetch_interaction_models(self, _event: wx.CommandEvent) -> None:
		idx = self._interaction_provider_choice.GetSelection()
		if idx == wx.NOT_FOUND or idx == 0:
			# Auto-detect selected or nothing — need a specific provider.
			# Translators: Spoken when no interaction provider is selected for model fetching.
			ui.message(_("Please select an interaction provider first."))
			return
		pdata = self._providers_data[idx - 1]  # -1 for auto-detect offset
		provider = provider_from_dict(pdata)

		self._interaction_fetch_btn.Enable(False)
		# Translators: Spoken when fetching models from a provider.
		ui.message(_("Fetching models..."))

		def _on_success(models: list[str]) -> None:
			self._interaction_model_choice.Clear()
			self._interaction_model_choice.AppendItems(models)
			self._interaction_fetch_btn.Enable(True)
			if models:
				self._interaction_model_choice.SetSelection(0)
				# Translators: Spoken when models have been fetched. {count} is the number found.
				ui.message(_("{count} model(s) found.").format(count=len(models)))
			else:
				# Translators: Spoken when no models were found.
				ui.message(_("No models found."))

		def _on_error(exc: Exception) -> None:
			self._interaction_fetch_btn.Enable(True)
			# Translators: Spoken when fetching models fails. {error} is the error detail.
			ui.message(_("Failed to fetch models: {error}").format(error=str(exc)))

		run_in_background(provider.fetch_models, on_success=_on_success, on_error=_on_error)

	def _on_ok(self, event: wx.CommandEvent) -> None:
		idx = self._provider_choice.GetSelection()
		provider_name = ""
		if idx != wx.NOT_FOUND:
			provider_name = self._providers_data[idx].get("name", "")

		luma_config.set_setting("default_provider", provider_name)
		luma_config.set_setting("default_model", self._model_choice.GetValue().strip())
		luma_config.set_setting("open_in_dialog", self._dialog_cb.GetValue())
		luma_config.set_setting("send_full_images", self._full_images_cb.GetValue())
		luma_config.set_setting("default_temperature", self._temperature.GetValue())
		luma_config.set_setting("timeout", self._timeout.GetValue())

		# Interaction mode settings.
		interaction_idx = self._interaction_provider_choice.GetSelection()
		interaction_provider = ""
		if interaction_idx > 0:  # 0 = auto-detect
			interaction_provider = self._providers_data[interaction_idx - 1].get("name", "")
		luma_config.set_setting("interaction_provider", interaction_provider)
		luma_config.set_setting("interaction_model", self._interaction_model_choice.GetValue().strip())

		# Speech language.
		luma_config.set_setting("speech_language", self._speech_language.GetValue().strip())

		luma_config.save()
		self.EndModal(wx.ID_OK)
