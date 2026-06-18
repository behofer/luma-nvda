"""Configuration manager for Luma.

Stores all user settings (providers, active skill, shortcuts, preferences)
in a JSON file within the NVDA user configuration directory.

Provider storage is split into two sections:

* ``preset_api_keys`` — a ``{preset_id: api_key}`` dict for well-known
  providers (OpenAI, Anthropic, Gemini, OpenRouter).  The preset
  definitions live in :mod:`providers.presets`.
* ``custom_providers`` — a list of user-defined OpenAI-compatible provider
  dicts (same shape as the old ``providers`` list).

.. note:: **Security limitation** — API keys are stored as plain text in
   ``luma.json``.  Anyone with read access to the NVDA user configuration
   directory can read them.  Do not share your NVDA config directory with
   untrusted parties.  A future version may use Windows DPAPI for
   encryption.
"""

import copy
import json
import logging
import os

import globalVars

log = logging.getLogger(__name__)

_CONFIG_FILENAME = "luma.json"

_DEFAULTS = {
	"preset_api_keys": {},
	"custom_providers": [],
	"active_skill": None,
	"shortcuts": {},
	"settings": {
		"open_in_dialog": True,
		"timeout": 60,
		"default_temperature": 0.3,
		"send_full_images": False,
	},
	"local_inference": {
		"model_variant": "",
		"model_path": "",
		"mmproj_path": "",
		"backend": "cpu",
	},
}

_data: dict | None = None


def _config_path() -> str:
	"""Return the full path to the Luma configuration file."""
	return os.path.join(globalVars.appArgs.configPath, _CONFIG_FILENAME)


def load() -> None:
	"""Load configuration from disk, falling back to defaults."""
	global _data
	path = _config_path()
	if os.path.isfile(path):
		try:
			with open(path, "r", encoding="utf-8") as fh:
				_data = json.load(fh)
			log.debug("Luma config loaded from %s", path)
		except (json.JSONDecodeError, OSError) as exc:
			log.error("Failed to load Luma config: %s", exc)
			_data = copy.deepcopy(_DEFAULTS)
	else:
		_data = copy.deepcopy(_DEFAULTS)
	# Merge any missing default keys (forward-compatibility).
	for key, value in _DEFAULTS.items():
		_data.setdefault(key, copy.deepcopy(value))
	if isinstance(_data.get("settings"), dict):
		for key, value in _DEFAULTS["settings"].items():
			_data["settings"].setdefault(key, value)
	# Migrate legacy "providers" list → custom_providers.
	_migrate_legacy_providers()


def _migrate_legacy_providers() -> None:
	"""Move the old ``providers`` list into ``custom_providers`` if present."""
	legacy = _data.get("providers")
	if not isinstance(legacy, list) or not legacy:
		return
	existing_names = {p.get("name") for p in _data.get("custom_providers", [])}
	for p in legacy:
		if p.get("name") not in existing_names:
			_data.setdefault("custom_providers", []).append(p)
	del _data["providers"]
	log.info("Migrated %d legacy provider(s) to custom_providers.", len(legacy))
	save()


def save() -> None:
	"""Persist configuration to disk."""
	if _data is None:
		return
	path = _config_path()
	try:
		with open(path, "w", encoding="utf-8") as fh:
			json.dump(_data, fh, indent="\t", ensure_ascii=False)
		log.debug("Luma config saved to %s", path)
	except OSError as exc:
		log.error("Failed to save Luma config: %s", exc)


# ---------------------------------------------------------------------------
# Accessor helpers
# ---------------------------------------------------------------------------

def get(key: str, default=None):
	"""Get a top-level config value."""
	if _data is None:
		return default
	return _data.get(key, default)


def set(key: str, value) -> None:
	"""Set a top-level config value (does NOT auto-save)."""
	if _data is None:
		load()
	_data[key] = value


def get_setting(key: str, default=None):
	"""Get a value from the ``settings`` sub-dict."""
	if _data is None:
		return default
	return _data.get("settings", {}).get(key, default)


def set_setting(key: str, value) -> None:
	"""Set a value in the ``settings`` sub-dict (does NOT auto-save)."""
	if _data is None:
		load()
	_data.setdefault("settings", {})[key] = value


# ---------------------------------------------------------------------------
# Preset provider helpers
# ---------------------------------------------------------------------------

def get_preset_api_keys() -> dict[str, str]:
	"""Return the ``{preset_id: api_key}`` mapping."""
	return get("preset_api_keys", {})


def get_preset_api_key(preset_id: str) -> str:
	"""Return the API key for *preset_id*, or ``""``."""
	return get_preset_api_keys().get(preset_id, "")


def set_preset_api_key(preset_id: str, api_key: str) -> None:
	"""Store an API key for a preset provider and save."""
	if _data is None:
		load()
	keys = _data.setdefault("preset_api_keys", {})
	if api_key:
		keys[preset_id] = api_key
	else:
		keys.pop(preset_id, None)
	save()


# ---------------------------------------------------------------------------
# Custom provider helpers
# ---------------------------------------------------------------------------

def get_custom_providers() -> list[dict]:
	"""Return the list of user-defined custom providers."""
	return get("custom_providers", [])


def add_custom_provider(provider: dict) -> None:
	"""Append a custom provider definition and save."""
	providers = get_custom_providers()
	providers.append(provider)
	set("custom_providers", providers)
	save()


def update_custom_provider(index: int, provider: dict) -> None:
	"""Update a custom provider by index and save."""
	providers = get_custom_providers()
	if 0 <= index < len(providers):
		providers[index] = provider
		set("custom_providers", providers)
		save()


def remove_custom_provider(index: int) -> None:
	"""Remove a custom provider by index and save."""
	providers = get_custom_providers()
	if 0 <= index < len(providers):
		providers.pop(index)
		set("custom_providers", providers)
		save()


# ---------------------------------------------------------------------------
# Combined "configured providers" view
# ---------------------------------------------------------------------------

def get_configured_providers() -> list[dict]:
	"""Return all providers that are ready for use (have an API key or are keyless).

	This merges preset providers (those with an API key set) with all
	custom providers into a single list of dicts compatible with
	:func:`providers.provider_from_dict`.

	Each dict contains at least ``type``, ``name``, ``api_url``, and
	``api_key``.  Preset-derived entries also carry ``preset_id``.
	"""
	from .providers.presets import PRESETS

	result: list[dict] = []

	# Preset providers with an API key configured.
	api_keys = get_preset_api_keys()
	for preset in PRESETS:
		key = api_keys.get(preset.id, "")
		if key:
			entry = {
				"type": preset.provider_type,
				"name": preset.name,
				"api_url": preset.api_url,
				"api_key": key,
				"preset_id": preset.id,
			}
			result.append(entry)

	# All custom providers.
	for p in get_custom_providers():
		result.append(p)

	return result
