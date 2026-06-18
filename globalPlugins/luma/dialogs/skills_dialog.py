"""Skill management dialogs for Luma."""

from __future__ import annotations

import logging
import os

import wx

import ui

from .. import config as luma_config
from ..providers import provider_from_dict
from ..skill import Skill, load_skills, CUSTOM_SKILLS_DIR
from ..worker import run_in_background

log = logging.getLogger(__name__)


class SkillsDialog(wx.Dialog):
	"""List, add, edit, and delete skills."""

	def __init__(self, parent: wx.Window | None = None) -> None:
		# Translators: Title of the skills management dialog.
		super().__init__(parent, title=_("Manage Skills"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self._skills: list[Skill] = []
		self._build_ui()
		self._refresh_list()
		self.CentreOnScreen()

	def _build_ui(self) -> None:
		panel = wx.Panel(self)
		sizer = wx.BoxSizer(wx.VERTICAL)

		# Translators: Label above the skills list.
		label = wx.StaticText(panel, label=_("&Skills:"))
		sizer.Add(label, flag=wx.LEFT | wx.TOP, border=10)

		self._list = wx.ListBox(panel)
		self._list.Bind(wx.EVT_LISTBOX, self._on_selection_changed)
		sizer.Add(self._list, proportion=1, flag=wx.EXPAND | wx.ALL, border=10)

		btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Button to create a new custom skill.
		self._btn_new = wx.Button(panel, label=_("&New Skill..."))
		self._btn_new.Bind(wx.EVT_BUTTON, self._on_new)
		btn_sizer.Add(self._btn_new, flag=wx.RIGHT, border=8)

		# Translators: Button to edit the selected custom skill.
		self._btn_edit = wx.Button(panel, label=_("&Edit Skill..."))
		self._btn_edit.Bind(wx.EVT_BUTTON, self._on_edit)
		btn_sizer.Add(self._btn_edit, flag=wx.RIGHT, border=8)

		# Translators: Button to delete the selected custom skill.
		self._btn_delete = wx.Button(panel, label=_("&Delete Skill"))
		self._btn_delete.Bind(wx.EVT_BUTTON, self._on_delete)
		btn_sizer.Add(self._btn_delete, flag=wx.RIGHT, border=8)

		# Translators: Button to close the dialog.
		close_btn = wx.Button(panel, id=wx.ID_CLOSE, label=_("Close"))
		close_btn.Bind(wx.EVT_BUTTON, lambda _e: self.Destroy())
		self.SetEscapeId(wx.ID_CLOSE)
		btn_sizer.Add(close_btn)

		sizer.Add(btn_sizer, flag=wx.ALIGN_RIGHT | wx.ALL, border=10)
		panel.SetSizer(sizer)
		self.SetSize((500, 400))
		self.SetMinSize((400, 300))

	def _refresh_list(self) -> None:
		self._skills = load_skills()
		self._list.Clear()
		for sk in self._skills:
			suffix = ""
			if sk.builtin:
				# Translators: Suffix shown after a builtin skill name.
				suffix = _(" (builtin)")
			self._list.Append(sk.name + suffix)
		if self._list.GetCount() > 0:
			self._list.SetSelection(0)
		self._update_buttons()

	def _on_selection_changed(self, _event: wx.CommandEvent) -> None:
		self._update_buttons()

	def _update_buttons(self) -> None:
		idx = self._list.GetSelection()
		is_custom = idx != wx.NOT_FOUND and not self._skills[idx].builtin
		self._btn_edit.Enable(is_custom)
		self._btn_delete.Enable(is_custom)

	def _on_new(self, _event: wx.CommandEvent) -> None:
		dlg = SkillEditDialog(self)
		if dlg.ShowModal() == wx.ID_OK:
			dlg.save_skill()
			self._refresh_list()
		dlg.Destroy()

	def _on_edit(self, _event: wx.CommandEvent) -> None:
		idx = self._list.GetSelection()
		if idx == wx.NOT_FOUND:
			return
		skill = self._skills[idx]
		if skill.builtin:
			return
		dlg = SkillEditDialog(self, skill=skill)
		if dlg.ShowModal() == wx.ID_OK:
			dlg.save_skill()
			self._refresh_list()
		dlg.Destroy()

	def _on_delete(self, _event: wx.CommandEvent) -> None:
		idx = self._list.GetSelection()
		if idx == wx.NOT_FOUND:
			return
		skill = self._skills[idx]
		if skill.builtin:
			return
		# Translators: Confirmation prompt when deleting a skill. {name} is the skill name.
		if wx.MessageBox(
			_("Are you sure you want to delete the skill \"{name}\"?").format(name=skill.name),
			# Translators: Title of the delete-skill confirmation dialog.
			_("Delete Skill"),
			wx.YES_NO | wx.ICON_WARNING,
			self,
		) == wx.YES:
			if skill.file_path and os.path.isfile(skill.file_path):
				try:
					os.remove(skill.file_path)
				except OSError:
					log.exception("Could not delete skill file: %s", skill.file_path)
			self._refresh_list()


class SkillEditDialog(wx.Dialog):
	"""Create or edit a custom skill (saved as a Markdown file)."""

	def __init__(
		self,
		parent: wx.Window,
		skill: Skill | None = None,
	) -> None:
		if skill:
			# Translators: Title when editing an existing skill.
			title = _("Edit Skill")
		else:
			# Translators: Title when creating a new skill.
			title = _("New Skill")
		super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self._skill = skill
		self._providers_data = luma_config.get_configured_providers()
		self._build_ui()
		if skill:
			self._load_values(skill)
		self.CentreOnScreen()

	def _build_ui(self) -> None:
		sizer = wx.BoxSizer(wx.VERTICAL)

		# Name
		# Translators: Label for the skill name field.
		sizer.Add(wx.StaticText(self, label=_("&Name:")), flag=wx.LEFT | wx.TOP, border=10)
		self._name = wx.TextCtrl(self)
		sizer.Add(self._name, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# Provider
		# Translators: Label for the skill provider combo box.
		sizer.Add(wx.StaticText(self, label=_("&Provider:")), flag=wx.LEFT | wx.TOP, border=10)
		provider_names = [_("Default")] + [p.get("name", "") for p in self._providers_data]
		self._provider = wx.Choice(self, choices=provider_names)
		self._provider.SetSelection(0)
		sizer.Add(self._provider, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# Model
		# Translators: Label for the skill model combo box.
		sizer.Add(wx.StaticText(self, label=_("&Model:")), flag=wx.LEFT | wx.TOP, border=10)
		model_sizer = wx.BoxSizer(wx.HORIZONTAL)
		self._model = wx.ComboBox(self, choices=[_("Default")], style=wx.CB_DROPDOWN)
		self._model.SetValue(_("Default"))
		model_sizer.Add(self._model, proportion=1, flag=wx.RIGHT, border=8)
		# Translators: Button to fetch available models from the selected provider.
		self._fetch_btn = wx.Button(self, label=_("&Fetch Models"))
		self._fetch_btn.Bind(wx.EVT_BUTTON, self._on_fetch_models)
		model_sizer.Add(self._fetch_btn)
		sizer.Add(model_sizer, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# Temperature
		# Translators: Label for the temperature spin control.
		sizer.Add(wx.StaticText(self, label=_("&Temperature (0-200, divided by 100):")), flag=wx.LEFT | wx.TOP, border=10)
		self._temperature = wx.SpinCtrl(self, min=0, max=200, initial=30)
		sizer.Add(self._temperature, flag=wx.LEFT | wx.RIGHT, border=10)

		# Language
		# Translators: Label for the skill language field.
		sizer.Add(wx.StaticText(self, label=_("&Language:")), flag=wx.LEFT | wx.TOP, border=10)
		self._language = wx.TextCtrl(self, value=_("Default"))
		sizer.Add(self._language, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		# Prompt
		# Translators: Label for the skill prompt field.
		sizer.Add(wx.StaticText(self, label=_("P&rompt:")), flag=wx.LEFT | wx.TOP, border=10)
		self._prompt = wx.TextCtrl(self, style=wx.TE_MULTILINE)
		sizer.Add(self._prompt, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
		sizer.Add(btn_sizer, flag=wx.EXPAND | wx.ALL, border=10)

		self.SetSizer(sizer)
		self.SetSize((550, 550))
		self.SetMinSize((450, 400))
		self._name.SetFocus()

	def _load_values(self, skill: Skill) -> None:
		self._name.SetValue(skill.name)
		self._temperature.SetValue(int(skill.temperature * 100))
		self._language.SetValue(skill.language)
		self._prompt.SetValue(skill.prompt)

		# Select provider.
		if skill.provider != "Default":
			for i, p in enumerate(self._providers_data):
				if p.get("name") == skill.provider:
					self._provider.SetSelection(i + 1)  # +1 for the "Default" entry
					break

		# Set model text.
		if skill.model != "Default":
			self._model.SetValue(skill.model)

	def _on_fetch_models(self, _event: wx.CommandEvent) -> None:
		pidx = self._provider.GetSelection()
		if pidx <= 0:
			# "Default" selected — need to resolve which provider that is.
			settings = luma_config.get("settings") or {}
			default_name = settings.get("default_provider", "")
			pdata = None
			for p in self._providers_data:
				if p.get("name") == default_name:
					pdata = p
					break
			if pdata is None:
				# Translators: Spoken when no default provider is configured.
				ui.message(_("No default provider configured. Please select a provider."))
				return
		else:
			pdata = self._providers_data[pidx - 1]

		provider = provider_from_dict(pdata)
		self._fetch_btn.Enable(False)
		# Translators: Spoken when fetching models.
		ui.message(_("Fetching models..."))

		def _on_success(models: list[str]) -> None:
			self._model.Clear()
			self._model.AppendItems([_("Default")] + models)
			self._fetch_btn.Enable(True)
			if models:
				self._model.SetSelection(1)
				# Translators: Spoken when models have been fetched. {count} is the number.
				ui.message(_("{count} model(s) found.").format(count=len(models)))
			else:
				self._model.SetSelection(0)
				# Translators: Spoken when no models were found.
				ui.message(_("No models found."))

		def _on_error(exc: Exception) -> None:
			self._fetch_btn.Enable(True)
			# Translators: Spoken when model fetching fails. {error} is the detail.
			ui.message(_("Failed to fetch models: {error}").format(error=str(exc)))

		run_in_background(provider.fetch_models, on_success=_on_success, on_error=_on_error)

	def save_skill(self) -> None:
		"""Write the skill as a Markdown file into the custom skills directory."""
		name = self._name.GetValue().strip()
		if not name:
			return

		pidx = self._provider.GetSelection()
		provider_val = "Default"
		if pidx > 0:
			provider_val = self._providers_data[pidx - 1].get("name", "Default")

		model_val = self._model.GetValue().strip()
		if model_val == _("Default") or not model_val:
			model_val = "Default"

		temperature = self._temperature.GetValue() / 100.0
		language = self._language.GetValue().strip() or "Default"
		prompt = self._prompt.GetValue().strip()

		content = (
			f"---\n"
			f"Name: {name}\n"
			f"Provider: {provider_val}\n"
			f"Model: {model_val}\n"
			f"Temperature: {temperature}\n"
			f"Language: {language}\n"
			f"---\n"
			f"# Prompt\n"
			f"{prompt}\n"
		)

		os.makedirs(CUSTOM_SKILLS_DIR, exist_ok=True)

		# If editing, overwrite the existing file.
		if self._skill and self._skill.file_path and os.path.isfile(self._skill.file_path):
			path = self._skill.file_path
		else:
			# Generate a safe filename from the name.
			safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in name)
			safe = safe.strip().replace(" ", "_").lower()
			path = os.path.join(CUSTOM_SKILLS_DIR, f"{safe}.md")

		try:
			with open(path, "w", encoding="utf-8") as fh:
				fh.write(content)
			log.debug("Saved skill to %s", path)
		except OSError:
			log.exception("Could not save skill file: %s", path)
