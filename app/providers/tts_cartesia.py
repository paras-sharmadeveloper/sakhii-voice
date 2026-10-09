"""Cartesia Sonic streaming TTS."""

import httpx
from pipecat.services.cartesia.tts import (
    _CARTESIA_API_VERSION,
    CartesiaTTSService,
    GenerationConfig,
    language_to_cartesia_language,
)

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

DEFAULT_MODEL = "sonic-3.6"


def build(choice, ctx: CallContext) -> CartesiaTTSService:
    if not choice.voice:
        raise ValueError("Cartesia needs a voice id (models.tts.voice)")
    settings = {
        "model": resolve_model(choice.model, DEFAULT_MODEL, {}),
        "voice": choice.voice,
        "language": language_enum(ctx.language),
        "generation_config": GenerationConfig(speed=max(0.6, min(1.5, choice.speed))),
    }
    settings.update(settings_overrides(CartesiaTTSService.Settings, choice.options))
    return CartesiaTTSService(
        api_key=ctx.secret("tts", "api_key", ctx.settings.cartesia_api_key),
        sample_rate=ctx.sample_rate,
        text_filters=ctx.text_filters,
        settings=CartesiaTTSService.Settings(**settings),
    )


INFO = ProviderInfo(
    name="Cartesia",
    models=model_list((DEFAULT_MODEL, "Sonic 3.6")),
    languages=supported_languages(language_to_cartesia_language),
    credentials=["api_key"],
    tuning=[Tuning("speed", "Speaking speed", 0.6, 1.5, 1.0, "x", 0.05)],
)


async def voices(creds: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    params: dict[str, str | int] = {"limit": 100}
    headers = {"X-API-Key": creds["api_key"], "Cartesia-Version": _CARTESIA_API_VERSION}
    async with httpx.AsyncClient(timeout=10) as http:
        for _ in range(20):  # pages
            r = await http.get("https://api.cartesia.ai/voices", headers=headers, params=params)
            r.raise_for_status()
            body = r.json()
            page = body if isinstance(body, list) else body.get("data", [])
            out += [{"id": v["id"], "name": v.get("name", ""), "language": v.get("language"),
                     "description": v.get("description", "")} for v in page]
            if isinstance(body, list) or not body.get("has_more") or not page:
                break
            params["starting_after"] = page[-1]["id"]
    return out
