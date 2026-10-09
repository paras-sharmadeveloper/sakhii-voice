"""Provider registry.

Adding a provider = one new file in this package with a `build(choice, ctx)`
function + one line below. Nothing else in the engine changes.

An entry is a module path (its `build` is used) or "module:function".
"""

import importlib
from collections.abc import Callable
from typing import Any

from app.providers.base import CallContext

STT: dict[str, str] = {
    "sarvam": "app.providers.stt_sarvam",
    "elevenlabs": "app.providers.stt_elevenlabs",
    "deepgram": "app.providers.stt_deepgram",
    "google": "app.providers.stt_google",
    "azure": "app.providers.stt_azure",
    "gladia": "app.providers.stt_gladia",
    "assemblyai": "app.providers.stt_assemblyai",
}

LLM: dict[str, str] = {
    "openai": "app.providers.llm_openai",
    "sarvam": "app.providers.llm_sarvam",
    "gemini": "app.providers.llm_gemini",
    "azure_openai": "app.providers.llm_azure_openai",
    "groq": "app.providers.llm_groq",
    "anthropic": "app.providers.llm_anthropic",
}

TTS: dict[str, str] = {
    "sarvam": "app.providers.tts_sarvam",
    "elevenlabs": "app.providers.tts_elevenlabs",
    "azure": "app.providers.tts_azure",
    "google": "app.providers.tts_google",
    "cartesia": "app.providers.tts_cartesia",
    "deepgram": "app.providers.tts_deepgram",
}

REGISTRY = {"stt": STT, "llm": LLM, "tts": TTS}


class UnknownProvider(ValueError):
    pass


def _builder(kind: str, provider: str) -> Callable[[Any, CallContext], Any]:
    try:
        entry = REGISTRY[kind][provider]
    except KeyError:
        raise UnknownProvider(f"no {kind} provider {provider!r}") from None
    module, _, func = entry.partition(":")
    return getattr(importlib.import_module(module), func or "build")


def build(kind: str, choice: Any, ctx: CallContext) -> Any:
    if not choice.model:
        default = ctx.settings.default_models.get(f"{kind}.{choice.provider}")
        if default:
            choice = choice.model_copy(update={"model": default})
    return _builder(kind, choice.provider)(choice, ctx)


def preload() -> None:
    """Import every provider at startup so the first call doesn't pay for it."""
    for kind, providers in REGISTRY.items():
        for name in providers:
            _builder(kind, name)
