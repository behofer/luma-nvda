"""Ollama provider for Luma.

Uses Ollama's native ``/api/chat`` endpoint instead of the
OpenAI-compatible layer.  Key optimisations for local inference:

* **Thinking disabled** — ``think: false`` prevents the model from
  emitting an internal reasoning trace, which dramatically reduces
  latency on thinking-capable models like Gemma 4.
* **Native image format** — images are sent as a ``base64`` list
  directly on the message object (no ``image_url`` wrapper).
* **Keep-alive** — ``keep_alive: "10m"`` tells Ollama to keep the
  model loaded in VRAM between requests so the next call doesn't
  pay the load penalty.
"""

from __future__ import annotations

import json
import logging

from .base import Provider, register_provider

log = logging.getLogger(__name__)

# How long Ollama should keep the model loaded after a request.
_KEEP_ALIVE = "10m"

# Context window — 4096 is plenty for single-image + prompt workflows
# and avoids allocating a huge KV cache (gemma4 defaults to 128K).
_NUM_CTX = 4096

# Maximum output tokens — caps generation so the model doesn't ramble.
# 1024 is generous for descriptions; prevents runaway generation.
_NUM_PREDICT = 1024


@register_provider
class OllamaProvider(Provider):
	"""Provider for a local Ollama instance using the native API."""

	def __init__(
		self,
		name: str,
		api_url: str,
		api_key: str = "",
	) -> None:
		self._name = name
		self._api_url = api_url.rstrip("/")
		self._api_key = api_key  # Unused for Ollama; kept for interface.

	# -- Provider interface --------------------------------------------------

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
		self._api_url = value.rstrip("/")

	@property
	def api_key(self) -> str:
		return self._api_key

	@api_key.setter
	def api_key(self, value: str) -> None:
		self._api_key = value

	# -- Serialisation -------------------------------------------------------

	@classmethod
	def _from_dict(cls, data: dict) -> "OllamaProvider":
		return cls(
			name=data.get("name", ""),
			api_url=data.get("api_url", ""),
			api_key=data.get("api_key", ""),
		)

	# -- URL helpers ---------------------------------------------------------

	def _base_url(self) -> str:
		"""Return the Ollama base URL, stripping any trailing path."""
		url = self._api_url
		for suffix in ("/v1", "/api"):
			if url.endswith(suffix):
				url = url[: -len(suffix)]
				break
		return url

	def _chat_url(self) -> str:
		"""Return the native ``/api/chat`` endpoint."""
		return f"{self._base_url()}/api/chat"

	def _tags_url(self) -> str:
		"""Return the native ``/api/tags`` endpoint for model listing."""
		return f"{self._base_url()}/api/tags"

	# -- Public API ----------------------------------------------------------

	def fetch_models(self, timeout: int = 15) -> list[str]:
		data = self._http_json(self._tags_url(), timeout=timeout)
		if isinstance(data, dict) and "models" in data:
			return sorted(
				item.get("model") or item.get("name", "")
				for item in data["models"]
			)
		return []

	def process_request(
		self,
		*,
		model: str,
		messages: list[dict],
		temperature: float = 0.3,
		image_base64: str | None = None,
		timeout: int = 30,
	) -> str:
		formatted = self._build_messages(messages, image_base64)
		payload: dict = {
			"model": model,
			"messages": formatted,
			"stream": False,
			"think": False,
			"keep_alive": _KEEP_ALIVE,
			"options": {
				"temperature": temperature,
				"num_ctx": _NUM_CTX,
				"num_predict": _NUM_PREDICT,
			},
		}
		body = json.dumps(payload).encode("utf-8")
		data = self._http_json(
			self._chat_url(),
			headers={"Content-Type": "application/json"},
			data=body,
			method="POST",
			timeout=timeout,
		)
		try:
			return data["message"]["content"]
		except (KeyError, TypeError) as exc:
			log.error("Unexpected Ollama API response: %s", data)
			raise ValueError(
				# Translators: Error when Ollama returns an unexpected response.
				_("Unexpected response from Ollama. Is it running?"),
			) from exc

	# -- Transcription -------------------------------------------------------

	@property
	def supports_transcription(self) -> bool:
		return True

	def transcribe_audio(
		self,
		wav_data: bytes,
		*,
		model: str = "",
		language: str = "",
		timeout: int = 30,
	) -> str:
		"""Transcribe audio via Ollama's OpenAI-compatible Whisper endpoint."""
		url = f"{self._base_url()}/v1/audio/transcriptions"
		body, content_type = self._build_multipart_audio(
			wav_data, language=language,
		)
		headers: dict[str, str] = {"Content-Type": content_type}
		data = self._http_json(
			url, headers=headers, data=body, method="POST",
			timeout=timeout,
		)
		text = data.get("text", "").strip()
		if not text:
			# Translators: Spoken when transcription returns empty text.
			raise RuntimeError(_("Transcription returned empty text."))
		return text

	@staticmethod
	def _build_multipart_audio(
		wav_data: bytes,
		model: str = "whisper-1",
		language: str = "",
	) -> tuple[bytes, str]:
		"""Build a ``multipart/form-data`` body for the Whisper endpoint."""
		boundary = "----LumaSpeechBoundary9k3m"
		parts: list[bytes] = []

		parts.append(f"--{boundary}\r\n".encode())
		parts.append(
			b'Content-Disposition: form-data; name="file"; filename="recording.wav"\r\n'
		)
		parts.append(b"Content-Type: audio/wav\r\n\r\n")
		parts.append(wav_data)
		parts.append(b"\r\n")

		parts.append(f"--{boundary}\r\n".encode())
		parts.append(b'Content-Disposition: form-data; name="model"\r\n\r\n')
		parts.append(model.encode())
		parts.append(b"\r\n")

		if language:
			parts.append(f"--{boundary}\r\n".encode())
			parts.append(b'Content-Disposition: form-data; name="language"\r\n\r\n')
			parts.append(language.encode())
			parts.append(b"\r\n")

		parts.append(f"--{boundary}--\r\n".encode())

		body = b"".join(parts)
		content_type = f"multipart/form-data; boundary={boundary}"
		return body, content_type

	# -- Internal ------------------------------------------------------------

	@staticmethod
	def _build_messages(
		messages: list[dict],
		image_base64: str | None,
	) -> list[dict]:
		"""Convert to Ollama's native message format.

		Ollama uses an ``images`` list of raw base64 strings on each
		message object, rather than OpenAI's ``image_url`` content parts.
		"""
		result: list[dict] = []
		image_attached = False
		for msg in messages:
			role = msg["role"]
			text = msg["content"]
			entry: dict = {"role": role, "content": text}
			if image_base64 and role == "user" and not image_attached:
				image_attached = True
				entry["images"] = [image_base64]
			result.append(entry)
		return result
