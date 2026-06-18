"""Google Gemini provider for Luma.

Uses the Gemini ``generateContent`` REST API directly.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.parse
import urllib.request

from .base import Provider, register_provider

log = logging.getLogger(__name__)


@register_provider
class GeminiProvider(Provider):
	"""Provider for the Google Gemini API."""

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
	def _from_dict(cls, data: dict) -> "GeminiProvider":
		return cls(
			name=data.get("name", ""),
			api_url=data.get("api_url", ""),
			api_key=data.get("api_key", ""),
		)

	# -- Helpers -------------------------------------------------------------

	def _key_param(self) -> str:
		"""Return the ``?key=...`` query string for authentication."""
		return urllib.parse.urlencode({"key": self._api_key})

	# -- Public API ----------------------------------------------------------

	def fetch_models(self, timeout: int = 15) -> list[str]:
		url = f"{self._api_url}/models?{self._key_param()}"
		data = self._http_json(url, timeout=timeout)
		# Response: {"models": [{"name": "models/gemini-2.0-flash", ...}]}
		if isinstance(data, dict) and "models" in data:
			models: list[str] = []
			for item in data["models"]:
				model_name = item.get("name", "")
				# Strip the "models/" prefix used by the API.
				if model_name.startswith("models/"):
					model_name = model_name[len("models/"):]
				if model_name:
					models.append(model_name)
			return sorted(models)
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
		contents, system_instruction = self._build_contents(
			messages, image_base64,
		)
		payload: dict = {
			"contents": contents,
			"generationConfig": {"temperature": temperature},
		}
		if system_instruction:
			payload["systemInstruction"] = {
				"parts": [{"text": system_instruction}],
			}

		body = json.dumps(payload).encode("utf-8")
		url = (
			f"{self._api_url}/models/{model}:generateContent"
			f"?{self._key_param()}"
		)
		data = self._http_json(
			url,
			headers={"Content-Type": "application/json"},
			data=body,
			method="POST",
			timeout=timeout,
		)
		try:
			# Response: {"candidates": [{"content": {"parts": [{"text": "..."}]}}]}
			for part in data["candidates"][0]["content"]["parts"]:
				if "text" in part:
					return part["text"]
			return ""
		except (KeyError, IndexError, TypeError) as exc:
			log.error("Unexpected Gemini API response: %s", data)
			raise ValueError(
				"Unexpected response from the Gemini API. "
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
		if not model:
			model = "gemini-2.0-flash"

		audio_b64 = base64.b64encode(wav_data).decode("ascii")

		prompt = (
			"Transcribe the following audio exactly as spoken. "
			"Output only the transcribed text, nothing else."
		)
		if language:
			prompt += f" The audio is in {language}."

		payload: dict = {
			"contents": [{
				"parts": [
					{
						"text": prompt,
					},
					{
						"inline_data": {
							"mime_type": "audio/wav",
							"data": audio_b64,
						},
					},
				],
			}],
			"generationConfig": {"temperature": 0.0},
		}

		body = json.dumps(payload).encode("utf-8")
		url = (
			f"{self._api_url}/models/{model}:generateContent"
			f"?{self._key_param()}"
		)
		data = self._http_json(
			url,
			headers={"Content-Type": "application/json"},
			data=body,
			method="POST",
			timeout=timeout,
		)

		try:
			for part in data["candidates"][0]["content"]["parts"]:
				if "text" in part:
					text = part["text"].strip()
					if text:
						return text
			# Translators: Spoken when transcription returns empty text.
			raise RuntimeError(_("Transcription returned empty text."))
		except (KeyError, IndexError, TypeError) as exc:
			log.error("Unexpected Gemini transcription response: %s", data)
			# Translators: Spoken when the Gemini transcription response is malformed.
			raise RuntimeError(
				_("Transcription failed: unexpected response from the Gemini API."),
			) from exc

	# -- Video analysis interface ------------------------------------------------

	@property
	def supports_video(self) -> bool:
		return True

	def upload_video(
		self,
		file_path: str,
		mime_type: str = "video/mp4",
		timeout: int = 300,
	) -> str:
		"""Upload a video via the Gemini File API (resumable upload).

		Returns the file URI for use in :meth:`process_video_request`.
		"""
		file_size = os.path.getsize(file_path)
		filename = os.path.basename(file_path)

		# Step 1 — Initiate resumable upload.
		init_url = f"{self._api_url}/upload/v1beta/files"
		init_headers = {
			"X-Goog-Upload-Protocol": "resumable",
			"X-Goog-Upload-Command": "start",
			"X-Goog-Upload-Header-Content-Length": str(file_size),
			"X-Goog-Upload-Header-Content-Type": mime_type,
			"Content-Type": "application/json",
			"x-goog-api-key": self._api_key,
		}
		metadata = json.dumps(
			{"file": {"display_name": filename}},
		).encode("utf-8")

		init_req = urllib.request.Request(
			init_url, data=metadata, headers=init_headers, method="POST",
		)
		try:
			with urllib.request.urlopen(init_req, timeout=30) as resp:
				upload_url = resp.headers.get("x-goog-upload-url")
		except Exception as exc:
			log.error("Gemini File API init failed: %s", exc)
			raise ConnectionError(
				_("Could not initiate video upload to Gemini."),
			) from exc

		if not upload_url:
			raise ConnectionError(
				_("Gemini did not return an upload URL."),
			)

		# Step 2 — Upload file data.
		with open(file_path, "rb") as f:
			file_data = f.read()

		upload_headers = {
			"Content-Length": str(file_size),
			"X-Goog-Upload-Offset": "0",
			"X-Goog-Upload-Command": "upload, finalize",
		}
		upload_req = urllib.request.Request(
			upload_url, data=file_data, headers=upload_headers, method="POST",
		)

		file_name_id = None
		try:
			with urllib.request.urlopen(upload_req, timeout=timeout) as resp:
				if resp.status == 200:
					res_json = json.loads(resp.read().decode("utf-8"))
					file_name_id = res_json.get("file", {}).get("name")
		except Exception as exc:
			log.error("Gemini file upload failed: %s", exc)
			raise ConnectionError(
				_("Video upload to Gemini failed."),
			) from exc

		if not file_name_id:
			raise ConnectionError(
				_("Gemini did not return a file identifier after upload."),
			)

		# Step 3 — Poll until the file is ACTIVE.
		check_url = f"{self._api_url}/v1beta/{file_name_id}"
		for _ in range(60):
			try:
				check_req = urllib.request.Request(
					check_url,
					headers={"x-goog-api-key": self._api_key},
				)
				with urllib.request.urlopen(check_req, timeout=10) as resp:
					state_data = json.loads(resp.read().decode("utf-8"))
					state = state_data.get("state")
					if state == "ACTIVE":
						file_uri = state_data.get("uri")
						if file_uri:
							return file_uri
						raise ConnectionError(
							_("Gemini file is active but returned no URI."),
						)
					if state == "FAILED":
						raise ConnectionError(
							_("Video processing failed on Gemini's side."),
						)
			except ConnectionError:
				raise
			except Exception:
				pass  # Transient error — retry.
			time.sleep(2)

		raise TimeoutError(
			_("Timed out waiting for Gemini to process the uploaded video."),
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
		"""Send a generateContent request with a video attachment."""
		contents, system_instruction = self._build_contents(
			messages, image_base64=None,
		)

		# Prepend the video as a file_data part to the first user turn.
		video_part = {
			"file_data": {
				"mime_type": "video/mp4",
				"file_uri": video_uri,
			},
		}
		for entry in contents:
			if entry.get("role") == "user":
				entry["parts"].insert(0, video_part)
				break
		else:
			# No user turn found — create one.
			contents.insert(0, {
				"role": "user",
				"parts": [video_part],
			})

		payload: dict = {
			"contents": contents,
			"generationConfig": {"temperature": temperature},
		}
		if system_instruction:
			payload["systemInstruction"] = {
				"parts": [{"text": system_instruction}],
			}

		body = json.dumps(payload).encode("utf-8")
		url = (
			f"{self._api_url}/models/{model}:generateContent"
			f"?{self._key_param()}"
		)
		data = self._http_json(
			url,
			headers={"Content-Type": "application/json"},
			data=body,
			method="POST",
			timeout=timeout,
		)
		try:
			for part in data["candidates"][0]["content"]["parts"]:
				if "text" in part:
					return part["text"]
			return ""
		except (KeyError, IndexError, TypeError) as exc:
			log.error("Unexpected Gemini video response: %s", data)
			raise ValueError(
				"Unexpected response from the Gemini API. "
				"The model may not support video analysis."
			) from exc

	# -- Interaction (Computer Use) interface ----------------------------------

	_DISPLAY_WIDTH = 1280
	_DISPLAY_HEIGHT = 800
	# Gemini uses a normalised 0-999 coordinate grid.
	_COORD_SIZE = 1000

	@property
	def supports_interaction(self) -> bool:
		return True

	def prepare_interaction(
		self, *, user_request: str, screenshot_b64: str,
	) -> dict:
		tools = [
			{
				"computer_use": {
					"environment": "ENVIRONMENT_BROWSER",
				},
			},
		]
		contents: list[dict] = [
			{
				"role": "user",
				"parts": [
					{"text": user_request},
					{
						"inline_data": {
							"mime_type": "image/png",
							"data": screenshot_b64,
						},
					},
				],
			},
		]
		return {
			"messages": contents,
			"tools": tools,
			"display_size": (self._DISPLAY_WIDTH, self._DISPLAY_HEIGHT),
			"coordinate_size": (self._COORD_SIZE, self._COORD_SIZE),
		}

	def send_interaction(
		self, *, model, messages, tools, max_tokens=4096, timeout=120,
	) -> dict:
		payload: dict = {
			"contents": messages,
			"tools": tools,
			"generationConfig": {
				"temperature": 0.3,
				"maxOutputTokens": max_tokens,
			},
		}
		body = json.dumps(payload).encode("utf-8")
		url = (
			f"{self._api_url}/models/{model}:generateContent"
			f"?{self._key_param()}"
		)
		data = self._http_json(
			url,
			headers={"Content-Type": "application/json"},
			data=body,
			method="POST",
			timeout=timeout,
		)

		# Parse the Gemini response into the normalised format.
		candidate = {}
		try:
			candidate = data["candidates"][0]
		except (KeyError, IndexError, TypeError):
			log.error("Unexpected Gemini interaction response: %s", data)
			return {
				"done": True,
				"text": [],
				"tool_calls": [],
				"raw_content": [],
			}

		parts = candidate.get("content", {}).get("parts", [])
		text_blocks: list[str] = []
		tool_calls: list[dict] = []

		for part in parts:
			if "text" in part:
				t = part["text"].strip()
				if t:
					text_blocks.append(t)
			elif "functionCall" in part:
				fc = part["functionCall"]
				action = self._normalise_gemini_action(
					fc.get("name", ""),
					fc.get("args", {}),
				)
				tool_calls.append({
					"id": fc.get("id", fc.get("name", "")),
					"name": fc.get("name", ""),
					"action": action,
				})

		return {
			"done": len(tool_calls) == 0,
			"text": text_blocks,
			"tool_calls": tool_calls,
			"raw_content": parts,
		}

	def append_interaction_turn(
		self, messages, *, raw_content, tool_results,
	) -> None:
		# Append the model's response.
		messages.append({"role": "model", "parts": raw_content})

		# Build function responses with screenshots.
		result_parts: list[dict] = []
		for tr in tool_results:
			result_parts.append({
				"functionResponse": {
					"name": tr["name"],
					"id": tr["id"],
					"response": {"outcome": "success"},
				},
			})
			result_parts.append({
				"inline_data": {
					"mime_type": "image/png",
					"data": tr["screenshot_b64"],
				},
			})
		messages.append({"role": "user", "parts": result_parts})

	# -- Gemini action normalisation ------------------------------------------

	@staticmethod
	def _normalise_gemini_action(name: str, args: dict) -> dict:
		"""Convert a Gemini function call to the common action format.

		The common format matches Anthropic's convention so that
		:mod:`interaction.actions` can execute it without knowing the
		provider.
		"""
		if name == "click_at":
			return {
				"action": "left_click",
				"coordinate": [args.get("x", 0), args.get("y", 0)],
			}
		if name == "type_text_at":
			return {
				"action": "type_at",
				"coordinate": [args.get("x", 0), args.get("y", 0)],
				"text": args.get("text", ""),
				"clear_before_typing": args.get("clear_before_typing", False),
				"press_enter": args.get("press_enter", False),
			}
		if name == "hover_at":
			return {
				"action": "mouse_move",
				"coordinate": [args.get("x", 0), args.get("y", 0)],
			}
		if name == "scroll_at":
			return {
				"action": "scroll",
				"coordinate": [args.get("x", 0), args.get("y", 0)],
				"scroll_direction": args.get("direction", "down"),
				"scroll_amount": max(1, int(args.get("magnitude", 300) / 100)),
			}
		if name == "scroll_document":
			return {
				"action": "scroll_page",
				"scroll_direction": args.get("direction", "down"),
			}
		if name == "key_combination":
			return {"action": "key", "text": args.get("keys", "")}
		if name == "drag_and_drop":
			return {
				"action": "left_click_drag",
				"coordinate": [args.get("x", 0), args.get("y", 0)],
				"target_coordinate": [
					args.get("destination_x", 0),
					args.get("destination_y", 0),
				],
			}
		if name == "wait_5_seconds":
			return {"action": "wait", "duration": 5}
		if name in ("go_back",):
			return {"action": "key", "text": "alt+left"}
		if name in ("go_forward",):
			return {"action": "key", "text": "alt+right"}
		# Fallback — pass through for logging.
		log.warning("Unknown Gemini action: %s(%s)", name, args)
		return {"action": name}

	# -- Internal ------------------------------------------------------------

	@staticmethod
	def _build_contents(
		messages: list[dict],
		image_base64: str | None,
	) -> tuple[list[dict], str]:
		"""Convert generic messages to Gemini's ``contents`` format.

		Returns ``(contents, system_instruction)`` where system messages
		are extracted into a separate string.
		"""
		system_parts: list[str] = []
		contents: list[dict] = []
		image_attached = False

		for msg in messages:
			role = msg["role"]
			text = msg["content"]

			if role == "system":
				system_parts.append(text)
				continue

			# Gemini uses "model" instead of "assistant".
			gemini_role = "model" if role == "assistant" else "user"
			parts: list[dict] = []

			if image_base64 and role == "user" and not image_attached:
				image_attached = True
				parts.append({
					"inline_data": {
						"mime_type": "image/png",
						"data": image_base64,
					},
				})

			parts.append({"text": text})
			contents.append({"role": gemini_role, "parts": parts})

		return contents, "\n\n".join(system_parts)
