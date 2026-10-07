"""Sarvam Bulbul streaming TTS (websocket)."""

from loguru import logger
from pipecat.services.sarvam.tts import TTS_MODEL_CONFIGS, SarvamTTSService

from app.providers.base import CallContext, language_enum, resolve_model, settings_overrides

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
        api_key=ctx.settings.sarvam_api_key,
        sample_rate=ctx.sample_rate,
        settings=SarvamTTSService.Settings(**settings),
        text_filters=ctx.text_filters,
    )
