"""Download manager for local inference assets.

Fetches official ``llama.cpp`` Windows binaries from the ggml-org
GitHub releases and Gemma 4 GGUF model files from HuggingFace.
HTTP Range resume and Content-Length verification are applied to
every download so multi-GB transfers survive flaky connections.

All public functions are safe to call from background threads.
Progress is reported via callbacks (routed to the main thread by
the caller).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from typing import Callable

from .hardware import MODEL_REGISTRY

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_BASE_DIR = os.path.join(os.environ.get("APPDATA", ""), "luma-local")
BIN_DIR = os.path.join(_BASE_DIR, "llama_bin")
MODELS_DIR = os.path.join(_BASE_DIR, "models")
_META_FILE = os.path.join(_BASE_DIR, "meta.json")


def _ensure_dirs() -> None:
	os.makedirs(BIN_DIR, exist_ok=True)
	os.makedirs(MODELS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Metadata persistence (tracks what's downloaded)
# ---------------------------------------------------------------------------

def _load_meta() -> dict:
	if os.path.isfile(_META_FILE):
		try:
			with open(_META_FILE, "r", encoding="utf-8") as fh:
				return json.load(fh)
		except (json.JSONDecodeError, OSError):
			pass
	return {}


def _save_meta(data: dict) -> None:
	_ensure_dirs()
	with open(_META_FILE, "w", encoding="utf-8") as fh:
		json.dump(data, fh, indent="\t", ensure_ascii=False)


def get_downloaded_model() -> str | None:
	"""Return the variant name of the currently downloaded model, or None."""
	return _load_meta().get("model_variant")


def get_model_path() -> str | None:
	"""Return path to the downloaded model GGUF, or None."""
	return _load_meta().get("model_path")


def get_mmproj_path() -> str | None:
	"""Return path to the downloaded mmproj GGUF, or None."""
	return _load_meta().get("mmproj_path")


def get_server_exe() -> str | None:
	"""Return path to ``llama-server.exe`` if installed, else None."""
	return _load_meta().get("server_exe")


def get_backend() -> str | None:
	"""Return the backend of the installed binary (cpu / vulkan / cuda)."""
	return _load_meta().get("binary_backend")


def is_binary_installed() -> bool:
	"""True if a llama.cpp binary with ``llama-server.exe`` is installed."""
	exe = get_server_exe()
	return bool(exe and os.path.isfile(exe))


# ---------------------------------------------------------------------------
# HTTP download with resume + progress
# ---------------------------------------------------------------------------

# 1 MiB read chunks.
_CHUNK_SIZE = 1024 * 1024


def download_file(
	url: str,
	dest_path: str,
	*,
	on_progress: Callable[[int, int], None] | None = None,
) -> None:
	"""Download *url* to *dest_path* with resume support.

	Parameters
	----------
	url:
		The URL to download from.
	dest_path:
		Local file path to write to.
	on_progress:
		Called with ``(bytes_downloaded, total_bytes)`` after each
		chunk.  *total_bytes* may be ``0`` if the server doesn't send
		``Content-Length``.
	"""
	os.makedirs(os.path.dirname(dest_path), exist_ok=True)

	# Resume from partial download.
	existing_size = 0
	if os.path.isfile(dest_path):
		existing_size = os.path.getsize(dest_path)

	headers: dict[str, str] = {}
	if existing_size > 0:
		headers["Range"] = f"bytes={existing_size}-"

	request = urllib.request.Request(url, headers=headers)

	try:
		response = urllib.request.urlopen(request, timeout=30)
	except urllib.error.HTTPError as exc:
		if exc.code == 416 and existing_size > 0:
			# Range not satisfiable — file is already complete.
			log.info("File already complete: %s", dest_path)
			if on_progress:
				on_progress(existing_size, existing_size)
			return
		raise

	content_length = response.headers.get("Content-Length")
	if response.status == 206:
		remaining = int(content_length) if content_length else 0
		total = existing_size + remaining
	else:
		total = int(content_length) if content_length else 0
		existing_size = 0  # Server sent full file; overwrite.

	mode = "ab" if response.status == 206 else "wb"
	downloaded = existing_size

	try:
		with open(dest_path, mode) as fh:
			while True:
				chunk = response.read(_CHUNK_SIZE)
				if not chunk:
					break
				fh.write(chunk)
				downloaded += len(chunk)
				if on_progress:
					on_progress(downloaded, total)
	finally:
		response.close()

	log.info("Downloaded %s (%d bytes)", dest_path, downloaded)


def _verify_download_size(path: str, url: str) -> None:
	"""Verify the downloaded file isn't truncated via HEAD Content-Length."""
	local_size = os.path.getsize(path)
	try:
		req = urllib.request.Request(url, method="HEAD")
		with urllib.request.urlopen(req, timeout=15) as resp:
			expected = resp.headers.get("Content-Length")
			if expected is not None:
				expected_size = int(expected)
				if local_size != expected_size:
					os.remove(path)
					raise ValueError(
						f"Download incomplete for {os.path.basename(path)}: "
						f"got {local_size} bytes, expected {expected_size}"
					)
	except (urllib.error.URLError, OSError, ValueError) as exc:
		if isinstance(exc, ValueError):
			raise
		log.warning("Could not verify download size for %s: %s", path, exc)


# ---------------------------------------------------------------------------
# llama.cpp binary download (ggml-org/llama.cpp GitHub releases)
# ---------------------------------------------------------------------------

_GH_LATEST = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"

# CUDA toolkit version used by the official prebuilts.  12.4 is broadly
# compatible and covers driver 550+ (the default on any current Windows
# install).  The cudart zip is paired 1:1 with the CUDA llama zip.
_CUDA_VERSION = "12.4"

# Asset-name patterns per backend.  {tag} is substituted with e.g.
# "b8826" before matching.
_ASSET_PATTERNS: dict[str, list[str]] = {
	"cpu": [r"^llama-{tag}-bin-win-cpu-x64\.zip$"],
	"vulkan": [r"^llama-{tag}-bin-win-vulkan-x64\.zip$"],
	"cuda": [
		rf"^llama-{{tag}}-bin-win-cuda-{_CUDA_VERSION}-x64\.zip$",
		rf"^cudart-llama-bin-win-cuda-{_CUDA_VERSION}-x64\.zip$",
	],
}


def _fetch_latest_release() -> dict:
	"""Return the GitHub ``/releases/latest`` payload for ggml-org/llama.cpp."""
	try:
		req = urllib.request.Request(
			_GH_LATEST,
			headers={"Accept": "application/vnd.github+json"},
		)
		with urllib.request.urlopen(req, timeout=30) as resp:
			return json.loads(resp.read().decode("utf-8"))
	except (urllib.error.URLError, OSError) as exc:
		raise RuntimeError(
			# Translators: Error fetching the llama.cpp release index.
			_("Could not fetch the inference engine release index. "
			  "Check your internet connection."),
		) from exc


def _find_assets(release: dict, backend: str) -> list[dict]:
	"""Return the asset dicts this *backend* needs from *release*.

	Raises ``RuntimeError`` if any required asset is missing.
	"""
	tag = release.get("tag_name", "")
	patterns = _ASSET_PATTERNS.get(backend)
	if patterns is None:
		raise ValueError(f"Unknown backend: {backend!r}")

	assets = release.get("assets", [])
	matched: list[dict] = []
	for pat in patterns:
		regex = re.compile(pat.format(tag=re.escape(tag)))
		for asset in assets:
			if regex.match(asset.get("name", "")):
				matched.append(asset)
				break
		else:
			raise RuntimeError(
				# Translators: Error when the llama.cpp release is
				# missing an expected binary.  {backend} is cpu/cuda/
				# vulkan, {tag} is the release version.
				_("No {backend} binary found in llama.cpp release "
				  "{tag}.").format(backend=backend, tag=tag),
			)
	return matched


def estimate_binary_size(backend: str) -> int:
	"""Return total download size in bytes for the *backend* binary.

	Queries the GitHub releases API — so requires internet.  On
	failure, returns ``0`` and the caller should degrade gracefully.
	"""
	try:
		release = _fetch_latest_release()
		assets = _find_assets(release, backend)
		return sum(a.get("size", 0) for a in assets)
	except (RuntimeError, ValueError, OSError):
		log.warning("Binary size estimate failed", exc_info=True)
		return 0


def download_and_extract_binary(
	backend: str,
	*,
	on_progress: Callable[[int, int], None] | None = None,
) -> None:
	"""Download and extract the llama.cpp binary for *backend*.

	For ``backend == "cuda"`` this also downloads and extracts the
	matching cudart redistributable zip into the same directory so the
	CUDA runtime DLLs sit next to ``llama-server.exe``.
	"""
	_ensure_dirs()

	release = _fetch_latest_release()
	tag = release.get("tag_name", "")
	assets = _find_assets(release, backend)

	# Reset the bin directory so leftover files from an older backend
	# or release don't shadow the new DLLs.
	if os.path.isdir(BIN_DIR):
		shutil.rmtree(BIN_DIR, ignore_errors=True)
	os.makedirs(BIN_DIR, exist_ok=True)

	# Download + extract each asset.  Aggregated progress across all
	# assets so the UI shows a single monotonic bar.
	total_bytes = sum(a.get("size", 0) for a in assets)
	cumulative = 0

	for asset in assets:
		url = asset["browser_download_url"]
		name = asset["name"]
		size = asset.get("size", 0)
		zip_path = os.path.join(tempfile.gettempdir(), name)

		log.info("Downloading %s (%d bytes)", name, size)

		start_cumulative = cumulative
		def _on_progress(done: int, total: int, _s=start_cumulative,
		                 _t=total_bytes) -> None:
			if on_progress is not None:
				on_progress(_s + done, _t)

		download_file(url, zip_path, on_progress=_on_progress)
		_verify_download_size(zip_path, url)
		cumulative += size

		log.info("Extracting %s to %s", name, BIN_DIR)
		_extract_zip_flat(zip_path, BIN_DIR)

		try:
			os.remove(zip_path)
		except OSError:
			pass

	# Locate llama-server.exe (the zips are flat — one directory level
	# deep at most).
	server_exe = _find_exe(BIN_DIR, "llama-server.exe")
	if server_exe is None:
		raise RuntimeError(
			# Translators: Error when the downloaded zip is missing the
			# expected server binary.
			_("The downloaded inference engine is missing "
			  "llama-server.exe."),
		)

	meta = _load_meta()
	meta["binary_backend"] = backend
	meta["binary_tag"] = tag
	meta["server_exe"] = server_exe
	_save_meta(meta)

	log.info("llama.cpp installed: %s (%s)", tag, backend)


def _extract_zip_flat(zip_path: str, dest_dir: str) -> None:
	"""Extract *zip_path* into *dest_dir* with Zip Slip protection."""
	abs_target = os.path.realpath(dest_dir)
	with zipfile.ZipFile(zip_path, "r") as zf:
		for info in zf.infolist():
			member_path = os.path.realpath(
				os.path.join(dest_dir, info.filename),
			)
			if (
				not member_path.startswith(abs_target + os.sep)
				and member_path != abs_target
			):
				raise ValueError(
					f"Zip contains unsafe path: {info.filename!r}"
				)
		zf.extractall(dest_dir)


def _find_exe(root: str, name: str) -> str | None:
	"""Walk *root* and return the first file matching *name* (any case)."""
	target = name.lower()
	for dirpath, _dirs, files in os.walk(root):
		for f in files:
			if f.lower() == target:
				return os.path.join(dirpath, f)
	return None


# ---------------------------------------------------------------------------
# Model download (Unsloth Gemma 4 GGUFs on HuggingFace)
# ---------------------------------------------------------------------------

_HF_BASE = "https://huggingface.co"


def _hf_download_url(repo: str, filename: str) -> str:
	return f"{_HF_BASE}/{repo}/resolve/main/{filename}"


def download_model(
	variant: str,
	*,
	on_progress: Callable[[int, int], None] | None = None,
) -> tuple[str, str]:
	"""Download a Gemma 4 GGUF + mmproj from HuggingFace.

	Returns ``(model_path, mmproj_path)``.
	"""
	_ensure_dirs()

	if variant not in MODEL_REGISTRY:
		raise ValueError(f"Unknown model variant: {variant!r}")

	info = MODEL_REGISTRY[variant]
	repo = info["repo"]

	model_url = _hf_download_url(repo, info["model_file"])
	mmproj_url = _hf_download_url(repo, info["mmproj_file"])

	# Use variant-scoped filenames so downloading a different variant
	# doesn't collide with (or overwrite) the previous mmproj.
	model_path = os.path.join(MODELS_DIR, info["model_file"])
	mmproj_path = os.path.join(
		MODELS_DIR, f"{variant}-{info['mmproj_file']}",
	)

	log.info("Downloading model: %s", model_url)
	download_file(model_url, model_path, on_progress=on_progress)
	_verify_download_size(model_path, model_url)

	log.info("Downloading mmproj: %s", mmproj_url)
	download_file(mmproj_url, mmproj_path, on_progress=on_progress)
	_verify_download_size(mmproj_path, mmproj_url)

	meta = _load_meta()
	meta["model_variant"] = variant
	meta["model_path"] = model_path
	meta["mmproj_path"] = mmproj_path
	_save_meta(meta)

	log.info("Model ready: %s at %s", variant, model_path)
	return model_path, mmproj_path


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def remove_model() -> None:
	"""Delete the downloaded model files and reset the model metadata."""
	meta = _load_meta()
	for key in ("model_path", "mmproj_path"):
		path = meta.get(key)
		if path and os.path.isfile(path):
			try:
				os.remove(path)
				log.info("Removed %s", path)
			except OSError:
				log.warning("Could not remove %s", path, exc_info=True)
	meta.pop("model_variant", None)
	meta.pop("model_path", None)
	meta.pop("mmproj_path", None)
	_save_meta(meta)
