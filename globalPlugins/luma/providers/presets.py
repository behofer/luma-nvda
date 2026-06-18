"""Preset provider definitions for Luma.

Each preset describes a well-known AI provider with pre-configured
URLs.  Users only need to supply an API key to activate them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
	"""A well-known provider with pre-configured endpoints."""

	id: str
	name: str
	provider_type: str  # Class name in the provider registry.
	api_url: str
	requires_key: bool = True


PRESETS: tuple[ProviderPreset, ...] = (
	ProviderPreset(
		id="openai",
		name="OpenAI",
		provider_type="OpenAICompatProvider",
		api_url="https://api.openai.com/v1",
	),
	ProviderPreset(
		id="anthropic",
		name="Anthropic",
		provider_type="AnthropicProvider",
		api_url="https://api.anthropic.com/v1",
	),
	ProviderPreset(
		id="gemini",
		name="Google Gemini",
		provider_type="GeminiProvider",
		api_url="https://generativelanguage.googleapis.com/v1beta",
	),
	ProviderPreset(
		id="openrouter",
		name="OpenRouter",
		provider_type="OpenAICompatProvider",
		api_url="https://openrouter.ai/api/v1",
	),
	ProviderPreset(
		id="ollama",
		name="Ollama",
		provider_type="OllamaProvider",
		api_url="http://localhost:11434",
		requires_key=False,
	),
	ProviderPreset(
		id="local",
		name="Local",
		provider_type="LocalProvider",
		api_url="",
		requires_key=False,
	),
)


def get_preset(preset_id: str) -> ProviderPreset | None:
	"""Return the preset with the given *preset_id*, or ``None``."""
	for p in PRESETS:
		if p.id == preset_id:
			return p
	return None
