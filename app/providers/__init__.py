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
}

LLM: dict[str, str] = {
    "openai": "app.providers.llm_openai",
    "sarvam": "app.providers.llm_sarvam",
}

TTS: dict[str, str] = {
    "sarvam": "app.providers.tts_sarvam",
    "elevenlabs": "app.providers.tts_elevenlabs",
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
    return _builder(kind, choice.provider)(choice, ctx)


def preload() -> None:
    """Import every provider at startup so the first call doesn't pay for it."""
    for kind, providers in REGISTRY.items():
        for name in providers:
            _builder(kind, name)
