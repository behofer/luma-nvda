"""Providers package for Luma.

Re-exports the public API so callers can do::

    from .providers import Provider, provider_from_dict, OpenAICompatProvider
"""

from .base import Provider, provider_from_dict  # noqa: F401
from .openai_compat import OpenAICompatProvider  # noqa: F401
from .anthropic import AnthropicProvider  # noqa: F401
from .gemini import GeminiProvider  # noqa: F401
from .ollama import OllamaProvider  # noqa: F401
from .local import LocalProvider  # noqa: F401
from .presets import PRESETS, get_preset, ProviderPreset  # noqa: F401
