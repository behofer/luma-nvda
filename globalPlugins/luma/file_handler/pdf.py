"""PDF file processing for Luma.

Renders individual PDF pages as images using PyMuPDF (``fitz``).
PyMuPDF must be bundled in the add-on's ``lib/`` directory.
"""

from __future__ import annotations

import base64
import logging

log = logging.getLogger(__name__)

try:
	import fitz  # PyMuPDF
except ImportError:
	fitz = None


def _require_fitz() -> None:
	"""Raise a user-friendly error if PyMuPDF is not available."""
	if fitz is None:
		# Translators: Spoken when PyMuPDF is missing for PDF support.
		raise RuntimeError(
			_("PDF support requires PyMuPDF. "
			  "Please install it or place it in the add-on's lib folder."),
		)


def get_page_count(path: str) -> int:
	"""Return the number of pages in a PDF file.

	Raises :class:`RuntimeError` if PyMuPDF is not available.
	Raises :class:`OSError` if the file cannot be opened.
	"""
	_require_fitz()
	with fitz.open(path) as doc:
		return len(doc)


def render_page(path: str, page: int, *, dpi: int = 200) -> str:
	"""Render a single PDF page to a base64-encoded PNG string.

	*page* is zero-based.

	Raises :class:`RuntimeError` if PyMuPDF is not available.
	Raises :class:`IndexError` if *page* is out of range.
	"""
	_require_fitz()
	with fitz.open(path) as doc:
		if page < 0 or page >= len(doc):
			raise IndexError(
				f"Page {page} out of range (document has {len(doc)} pages)."
			)
		pix = doc[page].get_pixmap(dpi=dpi)
		png_bytes = pix.tobytes("png")
	return base64.b64encode(png_bytes).decode("ascii")


def process_pdf(path: str) -> tuple[str, dict]:
	"""Validate a PDF and return metadata for the caller.

	Returns ``("pdf", {"path": path, "page_count": N})``.
	"""
	page_count = get_page_count(path)
	return "pdf", {"path": path, "page_count": page_count}
