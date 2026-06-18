"""File recognition for Luma.

Detects files selected in File Explorer and processes them.
Supports JPEG/PNG images and PDF documents; extensible via the
processor registry.

This package re-exports the public API so that existing callers
(``from . import file_handler``) continue to work unchanged.
"""

from __future__ import annotations

import logging
import os
import re

from .image import process_image, read_image_file
from .pdf import process_pdf
from .pptx import process_pptx

log = logging.getLogger(__name__)

# Maximum file size in bytes (10 MB) — applies to image files.
# PDFs have no size limit because they are processed page-by-page.
_MAX_FILE_SIZE = 10 * 1024 * 1024


class UnsupportedFileTypeError(Exception):
	"""Raised when a file's extension has no registered processor."""

	def __init__(self, ext: str) -> None:
		self.ext = ext
		# Translators: Error when a file type is not supported. {ext} is the file extension.
		super().__init__(_("Unsupported file type: {ext}").format(ext=ext))


# ------------------------------------------------------------------
# Processor registry
# ------------------------------------------------------------------

# Maps extension -> processor function.
# Each processor returns (content_type, data).
_PROCESSORS: dict[str, object] = {
	".jpg": process_image,
	".jpeg": process_image,
	".png": process_image,
	".pdf": process_pdf,
	".pptx": process_pptx,
	".ppt": process_pptx,
}

SUPPORTED_EXTENSIONS = set(_PROCESSORS.keys())

# Image-only extensions (used for features that only apply to photos).
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}

# Extensions that are exempt from the file-size limit (processed in chunks).
_SIZE_EXEMPT_EXTENSIONS = {".pdf", ".pptx", ".ppt"}


def is_supported_file(path: str) -> bool:
	"""Return ``True`` if the file extension is supported."""
	ext = os.path.splitext(path)[1].lower()
	return ext in SUPPORTED_EXTENSIONS


def is_size_exempt(path: str) -> bool:
	"""Return ``True`` if the file type is exempt from the size limit."""
	ext = os.path.splitext(path)[1].lower()
	return ext in _SIZE_EXEMPT_EXTENSIONS


def process_file(path: str) -> tuple[str, object]:
	"""Process a file and return ``(content_type, data)``.

	For images: ``("image", base64_string)``.
	For PDFs: ``("pdf", {"path": ..., "page_count": ...})``.

	Raises :class:`UnsupportedFileTypeError` if no processor exists.
	Raises :class:`OSError` if the file cannot be read.
	"""
	ext = os.path.splitext(path)[1].lower()
	processor = _PROCESSORS.get(ext)
	if processor is None:
		raise UnsupportedFileTypeError(ext)
	return processor(path)


# ------------------------------------------------------------------
# Explorer detection via COM
# ------------------------------------------------------------------

def get_explorer_selected_file() -> str | None:
	"""Return the full path of the selected file in File Explorer, or ``None``.

	Uses COM automation via comtypes (bundled with NVDA) to query the
	Shell for the focused item in the active Explorer window.  Falls back
	to checking the Desktop if no Explorer window matches.
	"""
	try:
		import api
		fg = api.getForegroundObject()
		if fg.appModule.appName != "explorer":
			return None
	except Exception:
		return None

	try:
		from comtypes.client import CreateObject as COMCreate
		shell = COMCreate("shell.application")
		fg_hwnd = fg.windowHandle
		for window in shell.Windows():
			try:
				if window.hwnd and window.hwnd == fg_hwnd:
					focused_item = window.Document.FocusedItem
					return focused_item.path
			except Exception:
				continue
	except Exception:
		log.debug("COM shell enumeration failed.", exc_info=True)

	# Desktop fallback.
	try:
		import api
		desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
		name = api.getDesktopObject().objectWithFocus().name
		path = os.path.join(desktop, name)
		if os.path.isfile(path):
			return path
	except Exception:
		pass

	return None


def get_explorer_selected_files() -> list[str]:
	"""Return the full paths of all selected files in File Explorer.

	Uses COM automation via comtypes (bundled with NVDA) to query the
	Shell for the selected items in the active Explorer window.  Falls
	back to :func:`get_explorer_selected_file` for single selection.
	"""
	try:
		import api
		fg = api.getForegroundObject()
		if fg.appModule.appName != "explorer":
			return []
	except Exception:
		return []

	try:
		from comtypes.client import CreateObject as COMCreate
		shell = COMCreate("shell.application")
		fg_hwnd = fg.windowHandle
		for window in shell.Windows():
			try:
				if window.hwnd and window.hwnd == fg_hwnd:
					selected = window.Document.SelectedItems()
					paths = []
					for i in range(selected.Count):
						item = selected.Item(i)
						if os.path.isfile(item.path):
							paths.append(item.path)
					return paths
			except Exception:
				continue
	except Exception:
		log.debug("COM shell enumeration for multi-select failed.", exc_info=True)

	# Desktop / single-item fallback.
	single = get_explorer_selected_file()
	return [single] if single else []


# ------------------------------------------------------------------
# Filename sanitisation
# ------------------------------------------------------------------

# Characters forbidden in Windows filenames.
_WINDOWS_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Reserved device names on Windows.
_WINDOWS_RESERVED_NAMES = frozenset({
	"CON", "PRN", "AUX", "NUL",
	"COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
	"LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})

# Common image extensions (used to strip extensions the LLM might include).
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".svg"}


def sanitize_filename(raw: str) -> str:
	"""Sanitise a raw LLM response into a valid Windows filename stem.

	Returns the name **without** an extension — the caller is
	responsible for appending the original file extension.
	"""
	# Take only the first line (LLM might add extra commentary).
	name = raw.strip().split("\n")[0].strip()
	# Strip common markdown formatting artefacts.
	name = name.strip("`*_")
	# Strip surrounding quotes.
	name = name.strip("\"'")
	# Remove a file extension the LLM may have appended.
	stem, ext = os.path.splitext(name)
	if ext.lower() in _IMAGE_EXTS:
		name = stem
	# Remove forbidden characters.
	name = _WINDOWS_FORBIDDEN_RE.sub("", name)
	# Strip leading/trailing dots and spaces (Windows disallows these).
	name = name.strip(". ")
	# Guard against reserved device names.
	if name.upper() in _WINDOWS_RESERVED_NAMES:
		name = f"{name}_file"
	# Truncate to a reasonable length.
	if len(name) > 200:
		name = name[:200].rstrip(". ")
	# Final fallback.
	if not name:
		name = "renamed"
	return name


def get_file_size(path: str) -> int | None:
	"""Return file size in bytes, or ``None`` if the file cannot be accessed."""
	try:
		return os.path.getsize(path)
	except OSError:
		return None
