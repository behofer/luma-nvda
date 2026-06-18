"""Provider base class, HTTP helpers, and serialisation registry.

Every concrete provider must subclass :class:`Provider` and be
registered via the :func:`register_provider` decorator so that
:func:`provider_from_dict` can re-hydrate it from stored config.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDER_REGISTRY: dict[str, type["Provider"]] = {}


def register_provider(cls: type["Provider"]) -> type["Provider"]:
	"""Class decorator that adds *cls* to the provider registry."""
	_PROVIDER_REGISTRY[cls.__name__] = cls
	return cls


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class Provider(ABC):
	"""Abstract base for all LLM providers."""

	@property
	@abstractmethod
	def name(self) -> str:
		"""Human-readable provider name."""

	@property
	@abstractmethod
	def api_url(self) -> str:
		"""Base URL for chat/completion requests."""

	@property
	@abstractmethod
	def api_key(self) -> str:
		"""API key (may be empty for keyless providers)."""

	@abstractmethod
	def fetch_models(self, timeout: int = 15) -> list[str]:
		"""Return a list of model ID strings available from this provider.

		Must NOT be called on the main thread.
		"""

	@abstractmethod
	def process_request(
		self,
		*,
		model: str,
		messages: list[dict],
		temperature: float = 0.3,
		image_base64: str | None = None,
		timeout: int = 30,
	) -> str:
		"""Send a chat-completion request and return the assistant's text.

		``messages`` follows the OpenAI format::

			[{"role": "user", "content": "..."}, ...]

		If *image_base64* is provided the first user message will include the
		image as a vision attachment.

		Must NOT be called on the main thread.
		"""

	# -- Transcription (speech-to-text) interface ------------------------------

	@property
	def supports_transcription(self) -> bool:
		"""Whether this provider supports audio transcription.

		Subclasses that support transcription override this to return
		``True`` and implement :meth:`transcribe_audio`.
		"""
		return False

	def transcribe_audio(
		self,
		wav_data: bytes,
		*,
		model: str = "",
		language: str = "",
		timeout: int = 30,
	) -> str:
		"""Transcribe WAV audio data to text.

		*wav_data* is a complete WAV file (with headers).
		*model* is the model to use for transcription; providers may
		ignore it if they use a dedicated transcription model (e.g.
		Whisper).
		*language* is an ISO-639-1 code (e.g. ``"en"``, ``"de"``);
		providers may use it as a hint to improve transcription accuracy.

		Returns the transcribed text string.

		Must NOT be called on the main thread.
		"""
		raise NotImplementedError(
			f"{self.name} does not support audio transcription."
		)

	# -- Video analysis interface ----------------------------------------------

	@property
	def supports_video(self) -> bool:
		"""Whether this provider supports native video analysis.

		Subclasses that support video override this to return ``True``
		and implement :meth:`upload_video` and
		:meth:`process_video_request`.
		"""
		return False

	def upload_video(
		self,
		file_path: str,
		mime_type: str = "video/mp4",
		timeout: int = 300,
	) -> str:
		"""Upload a video file and return a provider-specific URI.

		The returned URI can be passed to :meth:`process_video_request`
		as *video_uri*.

		Must NOT be called on the main thread.
		"""
		raise NotImplementedError(
			f"{self.name} does not support video upload."
		)

	def process_video_request(
		self,
		*,
		model: str,
		messages: list[dict],
		temperature: float = 0.3,
		video_uri: str,
		timeout: int = 300,
	) -> str:
		"""Send a chat-completion request with a video attachment.

		*video_uri* is either a direct URL (e.g. YouTube) that the
		provider can consume natively, or a URI returned by
		:meth:`upload_video`.

		Must NOT be called on the main thread.
		"""
		raise NotImplementedError(
			f"{self.name} does not support video analysis."
		)

	# -- Interaction (Computer Use) interface ----------------------------------

	@property
	def supports_interaction(self) -> bool:
		"""Whether this provider supports interaction (computer use) mode.

		Subclasses that support interaction override this to return
		``True`` and implement :meth:`prepare_interaction`,
		:meth:`send_interaction`, and :meth:`append_interaction_turn`.
		"""
		return False

	def prepare_interaction(
		self,
		*,
		user_request: str,
		screenshot_b64: str,
	) -> dict:
		"""Build initial state for an interaction loop.

		Returns a dict with keys:

		- ``messages`` — provider-specific message list
		- ``tools`` — provider-specific tool config
		- ``display_size`` — ``(width, height)`` target for screenshot
		  resizing before sending
		- ``coordinate_size`` — ``(width, height)`` of the coordinate
		  space used by the API (for scaling actions to screen)
		"""
		raise NotImplementedError(
			f"{self.name} does not support interaction mode."
		)

	def send_interaction(
		self,
		*,
		model: str,
		messages: list,
		tools,
		max_tokens: int = 4096,
		timeout: int = 120,
	) -> dict:
		"""Send an interaction request and return a normalised response.

		Returns a dict with keys:

		- ``done`` — ``True`` when the model finished (no more actions)
		- ``text`` — list of reasoning text strings
		- ``tool_calls`` — list of dicts, each with ``"id"`` (str) and
		  ``"action"`` (common action dict for :mod:`interaction.actions`)
		- ``raw_content`` — opaque provider-specific data; pass back to
		  :meth:`append_interaction_turn` unchanged
		"""
		raise NotImplementedError(
			f"{self.name} does not support interaction mode."
		)

	def append_interaction_turn(
		self,
		messages: list,
		*,
		raw_content,
		tool_results: list[dict],
	) -> None:
		"""Append the assistant turn and tool results to *messages*.

		Mutates *messages* in place.

		Parameters
		----------
		raw_content:
			The ``raw_content`` value from the last
			:meth:`send_interaction` response.
		tool_results:
			List of ``{"id": str, "name": str, "screenshot_b64": str}``
			dicts — one per executed tool call, each with a fresh
			screenshot captured after the action.
		"""
		raise NotImplementedError(
			f"{self.name} does not support interaction mode."
		)

	# -- Serialisation ---------------------------------------------------------

	def to_dict(self) -> dict:
		"""Serialise to a plain dict suitable for JSON storage."""
		return {
			"type": self.__class__.__name__,
			"name": self.name,
			"api_url": self.api_url,
			"api_key": self.api_key,
		}

	def __str__(self) -> str:
		return self.name

	# -- HTTP helper used by all subclasses ---------------------------------

	@staticmethod
	def _http_json(
		url: str,
		*,
		headers: dict[str, str] | None = None,
		data: bytes | None = None,
		method: str | None = None,
		timeout: int = 15,
		_max_retries: int = 5,
	) -> dict:
		"""Make an HTTP request and return the parsed JSON response.

		Automatically retries on HTTP 429 (rate limit) and 529
		(overloaded) with exponential back-off.  The ``Retry-After``
		header is respected when present.

		Raises :class:`ConnectionError` or :class:`TimeoutError` with
		user-friendly messages on failure.
		"""
		last_exc: urllib.error.HTTPError | None = None
		last_body: str = ""
		for attempt in range(_max_retries + 1):
			request = urllib.request.Request(
				url, data=data, headers=headers or {}, method=method,
			)
			try:
				with urllib.request.urlopen(request, timeout=timeout) as response:
					return json.loads(response.read().decode("utf-8"))
			except urllib.error.HTTPError as exc:
				error_body = ""
				try:
					error_body = exc.read().decode("utf-8", errors="replace")
				except Exception:
					pass

				if exc.code in (429, 529) and attempt < _max_retries:
					# Respect Retry-After header when the server sends one.
					retry_after = exc.headers.get("Retry-After") if exc.headers else None
					if retry_after is not None:
						try:
							delay = float(retry_after)
						except (ValueError, TypeError):
							delay = None
					else:
						delay = None
					if delay is None:
						# Exponential back-off: 1s, 2s, 4s, 8s, … + jitter.
						delay = min(2 ** attempt + random.random(), 30)
					log.warning(
						"Rate-limited (%s) by %s — retry %d/%d in %.1fs",
						exc.code, url, attempt + 1, _max_retries, delay,
					)
					time.sleep(delay)
					last_exc = exc
					last_body = error_body
					continue

				log.error(
					"HTTP %s from %s — body: %s", exc.code, url, error_body,
				)
				raise ConnectionError(
					_friendly_network_error(exc, error_body),
				) from exc
			except urllib.error.URLError as exc:
				log.exception("HTTP request to %s failed", url)
				raise ConnectionError(_friendly_network_error(exc)) from exc
			except TimeoutError:
				log.error("HTTP request timed out: %s", url)
				raise TimeoutError(_friendly_timeout_error(url)) from None

		# All retries exhausted — raise the last rate-limit error.
		log.error(
			"HTTP %s from %s after %d retries — body: %s",
			last_exc.code, url, _max_retries, last_body,
		)
		raise ConnectionError(
			_friendly_network_error(last_exc, last_body),
		) from last_exc


# ---------------------------------------------------------------------------
# User-friendly error messages
# ---------------------------------------------------------------------------

def _extract_api_message(error_body: str) -> str:
	"""Try to extract a human-readable message from a JSON error body.

	Most LLM APIs return ``{"error": {"message": "..."}}`` or
	``{"error": "..."}`` on failure.  Returns an empty string when
	the body cannot be parsed.
	"""
	if not error_body:
		return ""
	try:
		data = json.loads(error_body)
	except (json.JSONDecodeError, ValueError):
		return ""
	err = data.get("error", data)
	if isinstance(err, dict):
		return err.get("message", "")
	if isinstance(err, str):
		return err
	return ""


def _friendly_network_error(
	exc: urllib.error.URLError,
	error_body: str = "",
) -> str:
	"""Produce a user-friendly message from a network error."""
	reason = getattr(exc, "reason", None)
	if isinstance(exc, urllib.error.HTTPError):
		code = exc.code
		if code == 401:
			# Translators: Error when the API key is invalid (HTTP 401).
			return _("Authentication failed. Please check your API key.")
		if code == 403:
			# Translators: Error when the API key lacks permissions (HTTP 403).
			return _("Access denied. Your API key may lack the required permissions.")
		if code == 404:
			# Translators: Error when the API endpoint URL is wrong (HTTP 404).
			return _("API endpoint not found. Please check the API URL.")
		if code == 429:
			# Translators: Error when too many requests are sent (HTTP 429).
			return _("Rate limit exceeded. Please wait a moment and try again.")
		if 500 <= code < 600:
			# Translators: Error for server-side failures. {code} is the HTTP status code.
			return _("Server error ({code}). The provider may be experiencing issues.").format(code=code)
		# For 400 and other codes, include the API's error message when
		# available — it explains exactly what went wrong.
		api_msg = _extract_api_message(error_body)
		if api_msg:
			# Translators: HTTP error with detail from the API.
			# {code} is the status code, {detail} is the API message.
			return _("API error {code}: {detail}").format(
				code=code, detail=api_msg,
			)
		# Translators: Generic HTTP error. {code} is the HTTP status code.
		return _("HTTP error {code} from the API.").format(code=code)
	if reason:
		reason_str = str(reason)
		if "Connection refused" in reason_str:
			# Translators: Error when the API server refuses the connection.
			return _("Connection refused. Is the API server running?")
		if "Name or service not known" in reason_str or "getaddrinfo failed" in reason_str:
			# Translators: Error when the API server hostname cannot be resolved.
			return _("Could not resolve the API server address. Please check the URL.")
	# Translators: Generic network error. {error} is the technical detail.
	return _("Network error: {error}").format(error=str(exc))


def _friendly_timeout_error(url: str) -> str:
	"""Produce a user-friendly message for timeout errors."""
	# Translators: Error when an API request times out. {url} is the server address.
	return _("Request timed out. The server at {url} did not respond in time.").format(url=url)


# ---------------------------------------------------------------------------
# Serialisation / deserialisation
# ---------------------------------------------------------------------------

def provider_from_dict(data: dict) -> "Provider":
	"""Re-hydrate a :class:`Provider` from its ``to_dict()`` representation.

	Looks up the class by the ``"type"`` key in :data:`_PROVIDER_REGISTRY`.
	Each registered class must accept the dict's remaining fields as
	keyword arguments to its constructor.
	"""
	ptype = data.get("type", "OpenAICompatProvider")
	cls = _PROVIDER_REGISTRY.get(ptype)
	if cls is None:
		raise ValueError(f"Unknown provider type: {ptype!r}")
	# Build kwargs from the dict, excluding the "type" key itself and
	# any keys the class constructor doesn't expect (e.g. "preset_id").
	return cls._from_dict(data)
