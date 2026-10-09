"""Deepgram Aura streaming TTS. Aura voices are English and Spanish only."""

import httpx
from pipecat.services.deepgram.tts import DeepgramTTSService

from app.providers.base import CallContext, ProviderInfo, Tuning, model_list, settings_overrides

DEFAULT_VOICE = "aura-2-helena-en"


def build(choice, ctx: CallContext) -> DeepgramTTSService:
    settings = {"voice": choice.voice or DEFAULT_VOICE, "speed": max(0.7, min(1.5, choice.speed))}
    settings.update(settings_overrides(DeepgramTTSService.Settings, choice.options))
    return DeepgramTTSService(
        api_key=ctx.secret("tts", "api_key", ctx.settings.deepgram_api_key),
        sample_rate=ctx.sample_rate,
        text_filters=ctx.text_filters,
        settings=DeepgramTTSService.Settings(**settings),
    )


INFO = ProviderInfo(
    name="Deepgram Aura",
    models=model_list(("aura-2", "Aura 2")),
    # Pipecat has no language map for Deepgram TTS; Aura voices are English (and Spanish).
    languages=["en-IN"],
    credentials=["api_key"],
    tuning=[Tuning("speed", "Speaking speed", 0.7, 1.5, 1.0, "x", 0.05)],
    notes="English voices only: not for Hindi or other Indian-language agents.",
)


async def voices(creds: dict[str, str]) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get("https://api.deepgram.com/v1/models", headers={"Authorization": f"Token {creds['api_key']}"})
        r.raise_for_status()
    return [
        {"id": m.get("canonical_name"), "name": m.get("name"), "language": (m.get("languages") or [None])[0],
         "metadata": m.get("metadata") or {}}
        for m in r.json().get("tts", [])
        if str(m.get("canonical_name", "")).startswith("aura")
    ]
