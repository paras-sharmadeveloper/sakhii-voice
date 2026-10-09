"""Azure Speech TTS (neural voices; use an India region, e.g. centralindia)."""

import httpx
from pipecat.services.azure.common import language_to_azure_language
from pipecat.services.azure.tts import AzureTTSService

from app.providers.base import CallContext, ProviderInfo, Tuning, model_list, settings_overrides, supported_languages

# Azure's neural voice names are "<locale>-<Name>Neural".
DEFAULT_VOICES = {"hi-IN": "hi-IN-SwaraNeural", "en-IN": "en-IN-NeerjaNeural"}


def build(choice, ctx: CallContext) -> AzureTTSService:
    settings = {
        "voice": choice.voice or DEFAULT_VOICES.get(ctx.language, f"{ctx.language}-SwaraNeural"),
        "language": ctx.language,
        "rate": f"{max(0.5, min(2.0, choice.speed)):.2f}",
    }
    if "pitch" in choice.options:  # -50..50 (%)
        settings["pitch"] = f"{int(choice.options['pitch']):+d}%"
    settings.update(settings_overrides(AzureTTSService.Settings, {k: v for k, v in choice.options.items() if k != "pitch"}))
    return AzureTTSService(
        api_key=ctx.secret("tts", "api_key", ctx.settings.azure_speech_key),
        region=ctx.secret("tts", "region", ctx.settings.azure_speech_region),
        sample_rate=ctx.sample_rate,
        text_filters=ctx.text_filters,
        settings=AzureTTSService.Settings(**settings),
    )


INFO = ProviderInfo(
    name="Azure Speech",
    models=model_list(("neural", "Neural voices")),
    languages=supported_languages(language_to_azure_language),
    credentials=["api_key", "region"],
    tuning=[Tuning("speed", "Speaking speed", 0.5, 2.0, 1.0, "x", 0.05), Tuning("pitch", "Pitch", -50, 50, 0, "%")],
)


async def voices(creds: dict[str, str]) -> list[dict]:
    url = f"https://{creds['region']}.tts.speech.microsoft.com/cognitiveservices/voices/list"
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get(url, headers={"Ocp-Apim-Subscription-Key": creds["api_key"]})
        r.raise_for_status()
    return [
        {"id": v["ShortName"], "name": v.get("DisplayName", v["ShortName"]), "language": v.get("Locale"),
         "gender": v.get("Gender"), "styles": v.get("StyleList", [])}
        for v in r.json()
    ]
