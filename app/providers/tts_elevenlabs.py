"""ElevenLabs streaming TTS (multi-stream websocket)."""

import httpx
from pipecat.services.elevenlabs.stt import language_to_elevenlabs_language
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

from app.providers.base import (
    CallContext,
    ProviderInfo,
    Tuning,
    language_enum,
    model_list,
    resolve_model,
    settings_overrides,
    supported_languages,
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
        api_key=ctx.secret("tts", "api_key", ctx.settings.elevenlabs_api_key),
        sample_rate=ctx.sample_rate,
        # Lets ElevenLabs start audio without waiting to fill its chunk buffer.
        auto_mode=True,
        settings=ElevenLabsTTSService.Settings(**settings),
        text_filters=ctx.text_filters,
    )

INFO = ProviderInfo(
    name="ElevenLabs",
    models=model_list(
        (DEFAULT_MODEL, "Flash v2.5 (fastest)"),
        ("eleven_multilingual_v2", "Multilingual v2"),
    ),
    languages=supported_languages(language_to_elevenlabs_language),
    credentials=["api_key"],
    tuning=[
        Tuning("speed", "Speaking speed", 0.7, 1.2, 1.0, "x", 0.05),
        Tuning("stability", "Stability", 0, 100, 70, "%"),
        Tuning("similarity", "Similarity", 0, 100, 75, "%"),
        Tuning("style", "Style", 0, 100, 30, "%"),
    ],
)


async def voices(creds: dict[str, str]) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": creds["api_key"]})
        r.raise_for_status()
    return [
        {"id": v["voice_id"], "name": v.get("name", ""), "labels": v.get("labels") or {},
         "preview_url": v.get("preview_url")}
        for v in r.json().get("voices", [])
    ]
