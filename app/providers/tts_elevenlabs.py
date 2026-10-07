"""ElevenLabs streaming TTS (multi-stream websocket)."""

from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

from app.providers.base import (
    CallContext,
    language_enum,
    resolve_model,
    settings_overrides,
    unit_interval,
)

DEFAULT_MODEL = "eleven_flash_v2_5"
SLIDERS = ("stability", "similarity_boost", "style")


def build(choice, ctx: CallContext) -> ElevenLabsTTSService:
    options = dict(choice.options)
    # Builder calls it "Similarity".
    if "similarity" in options:
        options.setdefault("similarity_boost", options.pop("similarity"))
    for name in SLIDERS:
        if name in options:
            options[name] = unit_interval(options[name])

    settings = {
        "model": resolve_model(choice.model, DEFAULT_MODEL, {}),
        "voice": choice.voice,
        "speed": max(0.7, min(1.2, choice.speed)),
    }
    # Forcing a language makes Hinglish sound wrong, so only when asked.
    if options.pop("enforce_language", False):
        settings["language"] = language_enum(ctx.language)
    settings.update(settings_overrides(ElevenLabsTTSService.Settings, options))
    return ElevenLabsTTSService(
        api_key=ctx.settings.elevenlabs_api_key,
        sample_rate=ctx.sample_rate,
        # Lets ElevenLabs start audio without waiting to fill its chunk buffer.
        auto_mode=True,
        settings=ElevenLabsTTSService.Settings(**settings),
        text_filters=ctx.text_filters,
    )
