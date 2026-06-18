"""Shortcuts configuration dialog for Luma."""

from __future__ import annotations

import logging

import wx

from .. import config as luma_config
from ..skill import load_skills

log = logging.getLogger(__name__)

# The scopes a shortcut can target.
_SCOPES = [
	# Translators: Scope option — process the navigator object.
	_("Navigator object"),
	# Translators: Scope option — process the entire screen.
	_("Entire screen"),
	# Translators: Scope option — process the current application window.
	_("Current application"),
	# Translators: Scope option — open text chat.
	_("Text Chat"),
]

_SCOPE_KEYS = ["navigator", "screen", "app", "text_chat"]


class ShortcutsDialog(wx.Dialog):
	"""Map NVDA+Shift+1...0 to specific skill/scope combinations."""

	def __init__(self, parent: wx.Window | None = None) -> None:
		# Translators: Title of the shortcuts configuration dialog.
		super().__init__(parent, title=_("Configure Shortcuts"), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self._skills = load_skills()
		self._build_ui()
		self._load_values()
		self.CentreOnScreen()

	def _build_ui(self) -> None:
		sizer = wx.BoxSizer(wx.VERTICAL)

		# Translators: Instruction text in the shortcuts dialog.
		sizer.Add(
			wx.StaticText(self, label=_("Assign a skill and scope to each NVDA+Shift shortcut key (1-0):")),
			flag=wx.ALL, border=10,
		)

		skill_names = [_("(none)")] + [sk.name for sk in self._skills]
		scope_labels = [_("(none)")] + list(_SCOPES)

		self._rows: list[tuple[wx.Choice, wx.Choice]] = []
		grid = wx.FlexGridSizer(cols=3, hgap=8, vgap=6)
		grid.AddGrowableCol(1, 1)
		grid.AddGrowableCol(2, 1)

		# Keys 1-9 then 0.
		key_labels = [str(i) for i in range(1, 10)] + ["0"]
		for key_label in key_labels:
			# Translators: Label for a shortcut row. {key} is the digit key.
			lbl = wx.StaticText(self, label=_("NVDA+Shift+{key}:").format(key=key_label))
			grid.Add(lbl, flag=wx.ALIGN_CENTER_VERTICAL)

			skill_choice = wx.Choice(self, choices=skill_names)
			skill_choice.SetSelection(0)
			grid.Add(skill_choice, flag=wx.EXPAND)

			scope_choice = wx.Choice(self, choices=scope_labels)
			scope_choice.SetSelection(0)
			grid.Add(scope_choice, flag=wx.EXPAND)

			self._rows.append((skill_choice, scope_choice))

		sizer.Add(grid, proportion=1, flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10)

		btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
		sizer.Add(btn_sizer, flag=wx.EXPAND | wx.ALL, border=10)
		self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

		self.SetSizer(sizer)
		self.SetSize((600, 500))
		self.SetMinSize((500, 400))

	def _load_values(self) -> None:
		shortcuts = luma_config.get("shortcuts") or {}
		key_labels = [str(i) for i in range(1, 10)] + ["0"]
		skill_names = [sk.name for sk in self._skills]

		for i, key in enumerate(key_labels):
			mapping = shortcuts.get(key, {})
			skill_name = mapping.get("skill", "")
			scope_key = mapping.get("scope", "")

			skill_choice, scope_choice = self._rows[i]
			# Find skill index (+1 because index 0 is "(none)").
			if skill_name:
				try:
					idx = skill_names.index(skill_name) + 1
					skill_choice.SetSelection(idx)
				except ValueError:
					pass
			# Find scope index.
			if scope_key in _SCOPE_KEYS:
				scope_choice.SetSelection(_SCOPE_KEYS.index(scope_key) + 1)

	def _on_ok(self, event: wx.CommandEvent) -> None:
		shortcuts: dict[str, dict] = {}
		key_labels = [str(i) for i in range(1, 10)] + ["0"]

		for i, key in enumerate(key_labels):
			skill_choice, scope_choice = self._rows[i]
			skill_idx = skill_choice.GetSelection()
			scope_idx = scope_choice.GetSelection()

			if skill_idx > 0 and scope_idx > 0:
				skill_name = self._skills[skill_idx - 1].name
				scope_key = _SCOPE_KEYS[scope_idx - 1]
				shortcuts[key] = {"skill": skill_name, "scope": scope_key}

		luma_config.set("shortcuts", shortcuts)
		luma_config.save()
		self.EndModal(wx.ID_OK)
