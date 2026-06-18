"""WxPython dialogs for Luma.

All dialogs in this module follow NVDA conventions:
  - User-facing strings are wrapped in ``_()`` for gettext translation.
  - Network operations (model fetching, API calls) run in background
    threads with results dispatched back via ``wx.CallAfter``.

This package re-exports all public dialog classes so that existing
imports (``from .dialogs import XDialog``) continue to work.
"""

from .providers_dialog import ProvidersDialog, CustomProviderEditDialog
from .settings_dialog import SettingsDialog
from .skills_dialog import SkillsDialog, SkillEditDialog
from .shortcuts_dialog import ShortcutsDialog
from .text_chat_dialog import TextChatDialog
from .pdf_result_dialog import PdfResultDialog

__all__ = [
	"ProvidersDialog",
	"CustomProviderEditDialog",
	"SettingsDialog",
	"SkillsDialog",
	"SkillEditDialog",
	"ShortcutsDialog",
	"TextChatDialog",
	"PdfResultDialog",
]
