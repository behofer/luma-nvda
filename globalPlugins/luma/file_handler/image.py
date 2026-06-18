"""Image file processing for Luma.

Handles JPEG and PNG image files by reading and base64-encoding them.
"""

from __future__ import annotations

import base64
import logging

log = logging.getLogger(__name__)


def process_image(path: str) -> tuple[str, str]:
	"""Read a JPEG/PNG file and return ``("image", base64_string)``."""
	with open(path, "rb") as f:
		return "image", base64.b64encode(f.read()).decode("ascii")


def read_image_file(path: str) -> str | None:
	"""Read an image file and return its content as a base64 string, or ``None``."""
	try:
		with open(path, "rb") as f:
			return base64.b64encode(f.read()).decode("ascii")
	except Exception:
		log.exception("Failed to read image file: %s", path)
		return None
