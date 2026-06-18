"""OpenAI-compatible provider for Luma.

Works with OpenAI, OpenRouter, LM Studio, Ollama, and any other
endpoint that follows the OpenAI chat-completions API format.
"""

from __future__ import annotations

import json
import logging
import urllib.parse

from .base import Provider, register_provider

log = logging.getLogger(__name__)


@register_provider
class OpenAICompatProvider(Provider):
	"""Provider for any OpenAI-compatible API."""

	def __init__(
		self,
		name: str,
		api_url: str,
		api_key: str = "",
		model_fetch_url: str = "",
	) -> None:
		self._name = name
		self._api_url = api_url
		self._api_key = api_key
		self._model_fetch_url = model_fetch_url

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
		self._api_url = value

	@property
	def api_key(self) -> str:
		return self._api_key

	@api_key.setter
	def api_key(self, value: str) -> None:
		self._api_key = value

	@property
	def model_fetch_url(self) -> str:
		return self._model_fetch_url

	@model_fetch_url.setter
	def model_fetch_url(self, value: str) -> None:
		self._model_fetch_url = value

	# -- Serialisation -------------------------------------------------------

	def to_dict(self) -> dict:
		d = super().to_dict()
		d["model_fetch_url"] = self._model_fetch_url
		return d

	@classmethod
	def _from_dict(cls, data: dict) -> "OpenAICompatProvider":
		return cls(
			name=data.get("name", ""),
			api_url=data.get("api_url", ""),
			api_key=data.get("api_key", ""),
			model_fetch_url=data.get("model_fetch_url", ""),
		)

	# -- URL helpers ---------------------------------------------------------

	def _headers(self) -> dict[str, str]:
		headers = {"Content-Type": "application/json"}
		if self._api_key:
			headers["Authorization"] = f"Bearer {self._api_key}"
		return headers

	def _chat_url(self) -> str:
		"""Derive the chat completions endpoint from the base api_url."""
		url = self._api_url.rstrip("/")
		if url.endswith("/chat/completions"):
			return url
		# Ollama exposes its native API under /api but its OpenAI-compatible
		# endpoint under /v1.  When the URL ends with just /api and has no
		# deeper path, rewrite to /v1.
		parsed = urllib.parse.urlparse(url)
		if parsed.path.rstrip("/") == "/api":
			url = url.rsplit("/api", 1)[0] + "/v1"
		return url + "/chat/completions"

	def _models_url(self) -> str:
		"""Derive the model-listing endpoint from the base api_url."""
		if self._model_fetch_url:
			return self._model_fetch_url
		url = self._api_url.rstrip("/")
		parsed = urllib.parse.urlparse(url)
		if parsed.path.rstrip("/") == "/api":
			url = url.rsplit("/api", 1)[0] + "/v1"
		return url + "/models"

	# -- Public API ----------------------------------------------------------

	def fetch_models(self, timeout: int = 15) -> list[str]:
		data = self._http_json(
			self._models_url(), headers=self._headers(), timeout=timeout,
		)
		# OpenAI-style: {"data": [{"id": "gpt-4o"}, ...]}
		if isinstance(data, dict) and "data" in data:
			return sorted(item["id"] for item in data["data"])
		# Ollama-style: {"models": [{"model": "llama3"}, ...]}
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
		payload = {
			"model": model,
			"messages": formatted,
			"temperature": temperature,
		}
		body = json.dumps(payload).encode("utf-8")
		data = self._http_json(
			self._chat_url(),
			headers=self._headers(),
			data=body,
			method="POST",
			timeout=timeout,
		)
		try:
			return data["choices"][0]["message"]["content"]
		except (KeyError, IndexError, TypeError) as exc:
			log.error("Unexpected API response structure: %s", data)
			raise ValueError(
				"Unexpected response from the API. "
				"The model may not support this request format."
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
		url = self._transcription_url()
		body, content_type = self._build_multipart_audio(
			wav_data, language=language,
		)

		headers: dict[str, str] = {"Content-Type": content_type}
		if self._api_key:
			headers["Authorization"] = f"Bearer {self._api_key}"

		data = self._http_json(
			url, headers=headers, data=body, method="POST",
			timeout=timeout,
		)
		text = data.get("text", "").strip()
		if not text:
			# Translators: Spoken when transcription returns empty text.
			raise RuntimeError(_("Transcription returned empty text."))
		return text

	def _transcription_url(self) -> str:
		"""Derive the Whisper transcription endpoint from the base API URL."""
		url = self._api_url.rstrip("/")

		# Strip trailing chat/completions path if present.
		for suffix in ("/chat/completions", "/completions"):
			if url.endswith(suffix):
				url = url[: -len(suffix)]
				break

		# Ollama /api → /v1.
		parsed = urllib.parse.urlparse(url)
		if parsed.path.rstrip("/") == "/api":
			url = url.rsplit("/api", 1)[0] + "/v1"

		return url + "/audio/transcriptions"

	@staticmethod
	def _build_multipart_audio(
		wav_data: bytes,
		model: str = "whisper-1",
		language: str = "",
	) -> tuple[bytes, str]:
		"""Build a ``multipart/form-data`` body for the Whisper endpoint.

		Returns ``(body_bytes, content_type_header)``.
		"""
		boundary = "----LumaSpeechBoundary9k3m"
		parts: list[bytes] = []

		# Audio file part.
		parts.append(f"--{boundary}\r\n".encode())
		parts.append(
			b'Content-Disposition: form-data; name="file"; filename="recording.wav"\r\n'
		)
		parts.append(b"Content-Type: audio/wav\r\n\r\n")
		parts.append(wav_data)
		parts.append(b"\r\n")

		# Model part.
		parts.append(f"--{boundary}\r\n".encode())
		parts.append(b'Content-Disposition: form-data; name="model"\r\n\r\n')
		parts.append(model.encode())
		parts.append(b"\r\n")

		# Language hint (ISO-639-1).
		if language:
			parts.append(f"--{boundary}\r\n".encode())
			parts.append(b'Content-Disposition: form-data; name="language"\r\n\r\n')
			parts.append(language.encode())
			parts.append(b"\r\n")

		# Closing boundary.
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
		"""Convert simplified messages to OpenAI's multi-part format when an image is present."""
		result: list[dict] = []
		image_attached = False
		for msg in messages:
			role = msg["role"]
			text = msg["content"]
			if image_base64 and role == "user" and not image_attached:
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
