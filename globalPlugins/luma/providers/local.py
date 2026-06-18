"""Local inference provider for Luma.

Runs Gemma 4 on device via an ``llama-server.exe`` subprocess
(official ggml-org binary, downloaded on demand by the
:mod:`local_inference` package).  The subprocess exposes an
OpenAI-compatible HTTP API on ``127.0.0.1:<port>`` — this provider
just speaks that API.

Vision is handled by llama.cpp's ``libmtmd`` via a separate
``--mmproj`` GGUF loaded at server startup.  Images are attached as
base64 data URIs in the standard OpenAI ``image_url`` content part.

Audio transcription is stubbed until llama.cpp lands native Gemma 4
conformer audio support upstream.
"""

from __future__ import annotations

import json
import logging

from .base import Provider, register_provider

log = logging.getLogger(__name__)


@register_provider
class LocalProvider(Provider):
	"""Provider for on-device Gemma 4 inference via llama-server."""

	def __init__(
		self,
		name: str = "",
		api_url: str = "",
		api_key: str = "",
	) -> None:
		self._name = name or "Local"
		# api_url and api_key are unused but kept for interface compat.
		self._api_url = api_url
		self._api_key = api_key

	# -- Provider interface ------------------------------------------------

	@property
	def name(self) -> str:
		return self._name

	@name.setter
	def name(self, value: str) -> None:
		self._name = value

	@property
	def api_url(self) -> str:
		return self._api_url

	@api_url.setter
	def api_url(self, value: str) -> None:
		self._api_url = value

	@property
	def api_key(self) -> str:
		return self._api_key

	@api_key.setter
	def api_key(self, value: str) -> None:
		self._api_key = value

	# -- Serialisation -----------------------------------------------------

	@classmethod
	def _from_dict(cls, data: dict) -> "LocalProvider":
		return cls(
			name=data.get("name", ""),
			api_url=data.get("api_url", ""),
			api_key=data.get("api_key", ""),
		)

	# -- Model listing -----------------------------------------------------

	def fetch_models(self, timeout: int = 15) -> list[str]:
		"""Return the human-readable name of the downloaded variant."""
		from ..local_inference.downloader import get_downloaded_model
		from ..local_inference.hardware import variant_label

		variant = get_downloaded_model()
		if variant:
			return [variant_label(variant)]
		return []

	# -- Chat completion ---------------------------------------------------

	def process_request(
		self,
		*,
		model: str,
		messages: list[dict],
		temperature: float = 0.3,
		image_base64: str | None = None,
		timeout: int = 30,
	) -> str:
		"""Run inference locally by POSTing to the llama-server subprocess.

		The *model* parameter is accepted for interface compatibility but
		ignored — the model is whichever GGUF the server was started with.
		"""
		base_url = self._ensure_server_running()
		formatted = _build_messages(messages, image_base64)

		# llama-server accepts any model string in the request (it only
		# has one loaded), but sending something keeps the API parsers
		# on common providers happy.
		payload = {
			"model": "local",
			"messages": formatted,
			"temperature": temperature,
		}
		body = json.dumps(payload).encode("utf-8")

		# Local inference has no rate limits, but the default 30 s
		# timeout in callers is often too short for multi-GB models on
		# CPU.  Upgrade small timeouts to a local-friendly minimum.
		effective_timeout = max(timeout, 300)

		data = self._http_json(
			base_url + "/chat/completions",
			headers={"Content-Type": "application/json"},
			data=body,
			method="POST",
			timeout=effective_timeout,
		)

		# Mark the server as used so the idle timer re-arms.
		from ..local_inference.server_manager import get_manager
		get_manager().mark_used()

		try:
			return data["choices"][0]["message"]["content"]
		except (KeyError, IndexError, TypeError) as exc:
			log.error("Unexpected llama-server response: %s", data)
			raise ValueError(
				# Translators: Error when local model returns unexpected output.
				_("Unexpected response from the local model."),
			) from exc

	# -- Transcription (stub until llama.cpp adds Gemma 4 audio) -----------

	@property
	def supports_transcription(self) -> bool:
		"""Gemma 4 audio is not yet wired up in llama.cpp.  Pending upstream."""
		return False

	def transcribe_audio(
		self,
		wav_data: bytes,
		*,
		model: str = "",
		language: str = "",
		timeout: int = 30,
	) -> str:
		raise NotImplementedError(
			"Local audio transcription is not yet supported. "
			"Waiting for llama.cpp Gemma 4 conformer support."
		)

	# -- Internal ----------------------------------------------------------

	@staticmethod
	def _ensure_server_running() -> str:
		"""Start llama-server if needed and return its base URL."""
		from .. import config as luma_config
		from ..local_inference.downloader import (
			get_server_exe,
			get_backend,
		)
		from ..local_inference.server_manager import get_manager

		local_cfg = luma_config.get("local_inference", {})
		model_path = local_cfg.get("model_path", "")
		mmproj_path = local_cfg.get("mmproj_path", "")
		# Prefer the backend recorded at binary-install time — it must
		# match the binary on disk.  Fall back to the config value for
		# older setups.
		backend = get_backend() or local_cfg.get("backend", "cpu")
		server_exe = get_server_exe()

		if not server_exe or not model_path or not mmproj_path:
			raise RuntimeError(
				# Translators: Error when local inference is not set up.
				_("Local inference is not set up. Please run the setup "
				  "wizard first."),
			)

		manager = get_manager()
		manager.configure(
			server_exe=server_exe,
			model_path=model_path,
			mmproj_path=mmproj_path,
			backend=backend,
		)
		return manager.ensure_running()


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _build_messages(
	messages: list[dict],
	image_base64: str | None,
) -> list[dict]:
	"""Convert simplified messages to OpenAI multi-part format when needed.

	Mirrors :meth:`OpenAICompatProvider._build_messages` so a single
	image is attached to the first user message as a base64 ``image_url``
	data URI.
	"""
	if not image_base64:
		return messages

	result: list[dict] = []
	image_attached = False
	for msg in messages:
		role = msg["role"]
		text = msg["content"]
		if role == "user" and not image_attached:
			image_attached = True
			result.append({
				"role": "user",
				"content": [
					{"type": "text", "text": text},
					{
						"type": "image_url",
						"image_url": {
							"url": f"data:image/png;base64,{image_base64}",
						},
					},
				],
			})
		else:
			result.append({"role": role, "content": text})
	return result
