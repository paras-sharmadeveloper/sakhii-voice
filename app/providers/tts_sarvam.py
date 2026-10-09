"""Sarvam Bulbul streaming TTS (websocket)."""

from loguru import logger
from pipecat.services.sarvam.tts import TTS_MODEL_CONFIGS, SarvamTTSService, language_to_sarvam_language

from app.providers.base import (
    CallContext,
    ProviderInfo,
    Tuning,
    language_enum,
    model_list,
    resolve_model,
    settings_overrides,
    supported_languages,
)

DEFAULT_MODEL = "bulbul:v3"
# bulbul:v2 is retired upstream (requests fail).
ALIASES = {"bulbul:v2": DEFAULT_MODEL, "bulbul:v3-beta": DEFAULT_MODEL}
# bulbul:v2 speakers the builder used to offer -> closest bulbul:v3 speaker.
VOICE_ALIASES = {
    "anushka": "priya",
    "vidya": "ritu",
    "manisha": "neha",
    "arya": "pooja",
    "abhilash": "rahul",
    "karun": "aditya",
    "hitesh": "amit",
}


def _voice(requested: str | None, model: str) -> str:
    config = TTS_MODEL_CONFIGS.get(model, TTS_MODEL_CONFIGS[DEFAULT_MODEL])
    voice = (requested or "").strip().lower()
    voice = VOICE_ALIASES.get(voice, voice) if voice not in config.speakers else voice
    if voice not in config.speakers:
        logger.warning("Sarvam voice {!r} not in {}, using {}", requested, model, config.default_speaker)
        return config.default_speaker
    return voice


def build(choice, ctx: CallContext) -> SarvamTTSService:
    model = resolve_model(choice.model, DEFAULT_MODEL, ALIASES)
    settings = {
        "model": model,
        "voice": _voice(choice.voice, model),
        "language": language_enum(ctx.language),
        "pace": max(0.5, min(2.0, choice.speed)),
    }
    settings.update(settings_overrides(SarvamTTSService.Settings, choice.options))
    return SarvamTTSService(
        api_key=ctx.secret("tts", "api_key", ctx.settings.sarvam_api_key),
        sample_rate=ctx.sample_rate,
        settings=SarvamTTSService.Settings(**settings),
        text_filters=ctx.text_filters,
    )

INFO = ProviderInfo(
    name="Sarvam AI",
    models=model_list((DEFAULT_MODEL, "Bulbul v3")),
    languages=supported_languages(language_to_sarvam_language),
    credentials=["api_key"],
    tuning=[
        Tuning("speed", "Speaking speed", 0.5, 2.0, 1.0, "x", 0.05),
        Tuning("temperature", "Expressiveness", 0.01, 1.0, 0.6, step=0.01),
    ],
)


async def voices(creds: dict[str, str]) -> list[dict]:
    """Bulbul v3 speakers (Sarvam has no voices endpoint; the list is fixed per model)."""
    return [{"id": s, "name": s.title()} for s in TTS_MODEL_CONFIGS[DEFAULT_MODEL].speakers]
