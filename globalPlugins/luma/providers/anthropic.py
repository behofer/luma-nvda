"""Anthropic (Claude) provider for Luma.

Uses the Anthropic Messages API directly (not OpenAI-compatible).
"""

from __future__ import annotations

import json
import logging

from .base import Provider, register_provider

log = logging.getLogger(__name__)

# Anthropic API requires this header for versioning.
_API_VERSION = "2023-06-01"
_COMPUTER_USE_BETA = "computer-use-2025-11-24"


@register_provider
class AnthropicProvider(Provider):
	"""Provider for the Anthropic Messages API."""

	def __init__(
		self,
		name: str,
		api_url: str,
		api_key: str = "",
	) -> None:
		self._name = name
		self._api_url = api_url.rstrip("/")
		self._api_key = api_key

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
	def _from_dict(cls, data: dict) -> "AnthropicProvider":
		return cls(
			name=data.get("name", ""),
			api_url=data.get("api_url", ""),
			api_key=data.get("api_key", ""),
		)

	# -- Helpers -------------------------------------------------------------

	def _headers(self) -> dict[str, str]:
		return {
			"Content-Type": "application/json",
			"x-api-key": self._api_key,
			"anthropic-version": _API_VERSION,
		}

	# -- Public API ----------------------------------------------------------

	def fetch_models(self, timeout: int = 15) -> list[str]:
		url = f"{self._api_url}/models"
		data = self._http_json(url, headers=self._headers(), timeout=timeout)
		# Anthropic returns {"data": [{"id": "claude-..."}, ...]}
		if isinstance(data, dict) and "data" in data:
			return sorted(item["id"] for item in data["data"])
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
		system_text, api_messages = self._build_messages(messages, image_base64)
		payload: dict = {
			"model": model,
			"messages": api_messages,
			"temperature": temperature,
			"max_tokens": 4096,
		}
		if system_text:
			payload["system"] = system_text

		body = json.dumps(payload).encode("utf-8")
		url = f"{self._api_url}/messages"
		data = self._http_json(
			url, headers=self._headers(), data=body, method="POST",
			timeout=timeout,
		)
		try:
			# Response: {"content": [{"type": "text", "text": "..."}], ...}
			for block in data["content"]:
				if block.get("type") == "text":
					return block["text"]
			return ""
		except (KeyError, IndexError, TypeError) as exc:
			log.error("Unexpected Anthropic API response: %s", data)
			raise ValueError(
				"Unexpected response from the Anthropic API. "
				"The model may not support this request format."
			) from exc

	# -- Interaction (Computer Use) interface ----------------------------------

	_DISPLAY_WIDTH = 1280
	_DISPLAY_HEIGHT = 800

	@property
	def supports_interaction(self) -> bool:
		return True

	def prepare_interaction(
		self, *, user_request: str, screenshot_b64: str,
	) -> dict:
		tools = [
			{
				"type": "computer_20251124",
				"name": "computer",
				"display_width_px": self._DISPLAY_WIDTH,
				"display_height_px": self._DISPLAY_HEIGHT,
			},
		]
		messages: list[dict] = [
			{
				"role": "user",
				"content": [
					{"type": "text", "text": user_request},
					{
						"type": "image",
						"source": {
							"type": "base64",
							"media_type": "image/png",
							"data": screenshot_b64,
						},
					},
				],
			},
		]
		return {
			"messages": messages,
			"tools": tools,
			"display_size": (self._DISPLAY_WIDTH, self._DISPLAY_HEIGHT),
			"coordinate_size": (self._DISPLAY_WIDTH, self._DISPLAY_HEIGHT),
		}

	def send_interaction(
		self, *, model, messages, tools, max_tokens=4096, timeout=120,
	) -> dict:
		payload: dict = {
			"model": model,
			"messages": messages,
			"tools": tools,
			"max_tokens": max_tokens,
		}
		body = json.dumps(payload).encode("utf-8")
		url = f"{self._api_url}/messages"
		data = self._http_json(
			url,
			headers=self._computer_use_headers(),
			data=body,
			method="POST",
			timeout=timeout,
		)

		# Normalise the Anthropic response.
		content_blocks = data.get("content", [])
		stop_reason = data.get("stop_reason", "")

		text_blocks: list[str] = []
		tool_calls: list[dict] = []
		for block in content_blocks:
			btype = block.get("type", "")
			if btype == "text":
				t = block.get("text", "").strip()
				if t:
					text_blocks.append(t)
			elif btype == "tool_use":
				tool_calls.append({
					"id": block.get("id", ""),
					"name": block.get("name", "computer"),
					"action": block.get("input", {}),
				})

		return {
			"done": stop_reason == "end_turn" or not tool_calls,
			"text": text_blocks,
			"tool_calls": tool_calls,
			"raw_content": content_blocks,
		}

	def append_interaction_turn(
		self, messages, *, raw_content, tool_results,
	) -> None:
		messages.append({"role": "assistant", "content": raw_content})
		results: list[dict] = []
		for tr in tool_results:
			results.append({
				"type": "tool_result",
				"tool_use_id": tr["id"],
				"content": [
					{
						"type": "image",
						"source": {
							"type": "base64",
							"media_type": "image/png",
							"data": tr["screenshot_b64"],
						},
					},
				],
			})
		messages.append({"role": "user", "content": results})

	# -- Internal helpers for Computer Use ------------------------------------

	def _computer_use_headers(self) -> dict[str, str]:
		"""Return headers with the Computer Use beta flag."""
		headers = self._headers()
		headers["anthropic-beta"] = _COMPUTER_USE_BETA
		return headers

	# -- Internal ------------------------------------------------------------

	@staticmethod
	def _build_messages(
		messages: list[dict],
		image_base64: str | None,
	) -> tuple[str, list[dict]]:
		"""Convert generic messages to Anthropic's format.

		Returns ``(system_text, api_messages)`` where system messages
		are extracted into a separate string (Anthropic uses a top-level
		``system`` parameter rather than a system role in messages).
		"""
		system_parts: list[str] = []
		api_messages: list[dict] = []
		image_attached = False

		for msg in messages:
			role = msg["role"]
			text = msg["content"]

			if role == "system":
				system_parts.append(text)
				continue

			# Map "assistant" role (already correct for Anthropic).
			if image_base64 and role == "user" and not image_attached:
				image_attached = True
				api_messages.append({
					"role": "user",
					"content": [
						{
							"type": "image",
							"source": {
								"type": "base64",
								"media_type": "image/png",
								"data": image_base64,
							},
						},
						{"type": "text", "text": text},
					],
				})
			else:
				api_messages.append({"role": role, "content": text})

		return "\n\n".join(system_parts), api_messages
