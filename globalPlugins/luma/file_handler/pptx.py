"""PowerPoint file processing for Luma.

Converts PPTX/PPT presentations to a temporary PDF using PowerPoint
COM automation, then delegates page rendering to :mod:`pdf`.  PowerPoint
must be installed on the system.
"""

from __future__ import annotations

import logging
import os
import tempfile

log = logging.getLogger(__name__)


def _convert_to_pdf(pptx_path: str) -> str:
	"""Convert a PPTX/PPT file to a temporary PDF via PowerPoint COM.

	Returns the path to the generated PDF file.

	Raises :class:`RuntimeError` if PowerPoint is not installed or the
	conversion fails.

	Must NOT be called on the main thread (COM + blocking I/O).
	"""
	abs_path = os.path.abspath(pptx_path)
	if not os.path.isfile(abs_path):
		raise FileNotFoundError(abs_path)

	try:
		import comtypes.client
	except ImportError:
		# Translators: Spoken when comtypes is missing (should never happen in NVDA).
		raise RuntimeError(_("comtypes is required for PowerPoint support."))

	# ppSaveAsPDF = 32
	_PP_SAVE_AS_PDF = 32

	temp_dir = tempfile.mkdtemp(prefix="luma_pptx_")
	pdf_path = os.path.join(temp_dir, "slides.pdf")

	comtypes.CoInitialize()
	ppt = None
	pres = None
	try:
		try:
			ppt = comtypes.client.CreateObject("PowerPoint.Application")
		except Exception as exc:
			log.error("Could not start PowerPoint: %s", exc)
			# Translators: Spoken when PowerPoint is not installed.
			raise RuntimeError(
				_("PowerPoint is required to process presentation files. "
				  "Please install Microsoft PowerPoint."),
			) from exc

		try:
			pres = ppt.Presentations.Open(
				abs_path, ReadOnly=True, Untitled=False, WithWindow=False,
			)
			pres.SaveAs(pdf_path, _PP_SAVE_AS_PDF)
		except Exception as exc:
			log.error("PowerPoint conversion failed: %s", exc)
			# Translators: Spoken when PPTX to PDF conversion fails.
			raise RuntimeError(
				_("Failed to convert the presentation to PDF: {error}").format(
					error=str(exc),
				),
			) from exc
		finally:
			if pres is not None:
				try:
					pres.Close()
				except Exception:
					pass
			if ppt is not None:
				try:
					ppt.Quit()
				except Exception:
					pass
	finally:
		comtypes.CoUninitialize()

	if not os.path.isfile(pdf_path):
		raise RuntimeError(_("PowerPoint conversion produced no output."))

	return pdf_path


def get_slide_count(path: str) -> int:
	"""Return the number of slides by converting to PDF and counting pages.

	This is a relatively expensive operation (starts PowerPoint).
	Prefer calling :func:`process_pptx` which returns both the temp PDF
	path and the page count in a single conversion.
	"""
	from .pdf import get_page_count

	pdf_path = _convert_to_pdf(path)
	try:
		return get_page_count(pdf_path)
	finally:
		_cleanup_temp(pdf_path)


def render_slide(pdf_path: str, slide: int, *, dpi: int = 200) -> str:
	"""Render a slide from the pre-converted temporary PDF.

	*pdf_path* is the path returned by :func:`process_pptx` in the
	``pdf_path`` metadata field.
	*slide* is zero-based.

	Returns a base64-encoded PNG string.
	"""
	from .pdf import render_page

	return render_page(pdf_path, slide, dpi=dpi)


def process_pptx(path: str) -> tuple[str, dict]:
	"""Convert a PPTX/PPT to a temporary PDF and return metadata.

	Returns ``("pptx", {"path": path, "pdf_path": ..., "page_count": N})``.

	The caller is responsible for cleaning up *pdf_path* when done
	(see :func:`cleanup_temp`).
	"""
	from .pdf import get_page_count

	pdf_path = _convert_to_pdf(path)
	try:
		page_count = get_page_count(pdf_path)
	except Exception:
		_cleanup_temp(pdf_path)
		raise
	return "pptx", {"path": path, "pdf_path": pdf_path, "page_count": page_count}


def cleanup_temp(pdf_path: str) -> None:
	"""Remove the temporary PDF and its parent directory.

	Safe to call even if files have already been removed.
	"""
	_cleanup_temp(pdf_path)


def _cleanup_temp(pdf_path: str) -> None:
	"""Internal cleanup helper."""
	try:
		if os.path.isfile(pdf_path):
			os.unlink(pdf_path)
		parent = os.path.dirname(pdf_path)
		if os.path.isdir(parent) and parent.startswith(
			os.path.join(tempfile.gettempdir(), "luma_pptx_"),
		):
			os.rmdir(parent)
	except OSError:
		log.debug("Temp cleanup failed for %s", pdf_path, exc_info=True)
