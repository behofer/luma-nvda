"""Local inference package for Luma.

Provides on-device Gemma 4 inference by spawning the official
``llama-server.exe`` from ggml-org/llama.cpp as a subprocess and
speaking its OpenAI-compatible HTTP API.  The binary and GGUF model
files are downloaded on demand into ``%APPDATA%\\luma-local\\``.

Public API
----------
- :func:`is_available` — True if the binary is installed and the
  selected model files are on disk.
- :func:`is_setup_complete` — alias of :func:`is_available` (kept for
  legacy call sites).
- :func:`get_status` — human-readable setup status string.
- :func:`shutdown` — stop the server subprocess (call on NVDA exit).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status checks
# ---------------------------------------------------------------------------

def is_available() -> bool:
	"""Return True if the llama-server binary and a model are installed."""
	from .downloader import (
		is_binary_installed,
		get_model_path,
		get_mmproj_path,
	)

	if not is_binary_installed():
		return False
	model = get_model_path()
	mmproj = get_mmproj_path()
	if not model or not mmproj:
		return False
	return os.path.isfile(model) and os.path.isfile(mmproj)


def is_setup_complete() -> bool:
	"""Alias kept for legacy callers."""
	return is_available()


def get_status() -> str:
	"""Return a human-readable status string for the local inference setup."""
	from .downloader import (
		is_binary_installed,
		get_downloaded_model,
	)
	from .hardware import variant_label

	if not is_binary_installed():
		# Translators: Status when the llama.cpp binary isn't downloaded.
		return _("Not set up — download required")

	model = get_downloaded_model()
	if not model:
		# Translators: Status when the engine is installed but no model
		# is downloaded.
		return _("Engine installed — model download required")

	if not is_available():
		# Translators: Status when model files are missing / incomplete.
		return _("Model files incomplete — re-download required")

	# Translators: Status when local inference is fully set up.
	# {model} is the human-readable variant name, e.g. "Gemma 4 E4B".
	return _("Ready — {model}").format(model=variant_label(model))


# ---------------------------------------------------------------------------
# Shutdown hook
# ---------------------------------------------------------------------------

def shutdown() -> None:
	"""Stop the llama-server subprocess if it's running.

	Called from :meth:`GlobalPlugin.terminate` on NVDA exit so the child
	doesn't outlive the parent.  Safe to call even if the server was
	never started.
	"""
	try:
		from .server_manager import get_manager
		get_manager().stop()
	except Exception:
		log.warning("Error during local inference shutdown", exc_info=True)
