"""Google Cloud Text-to-Speech (streaming, Chirp 3 HD voices)."""

import json

from pipecat.services.google.tts import GoogleTTSService, language_to_google_tts_language

from app.providers.base import CallContext, ProviderInfo, Tuning, model_list, settings_overrides, supported_languages


def build(choice, ctx: CallContext) -> GoogleTTSService:
    settings = {
        "voice": choice.voice or f"{ctx.language}-Chirp3-HD-Kore",
        "language": ctx.language,
        "speaking_rate": max(0.5, min(2.0, choice.speed)),
    }
    settings.update(settings_overrides(GoogleTTSService.Settings, choice.options))
    return GoogleTTSService(
        credentials=ctx.secret("tts", "credentials_json", ctx.settings.google_credentials_json) or None,
        credentials_path=ctx.settings.google_credentials_path or None,
        sample_rate=ctx.sample_rate,
        text_filters=ctx.text_filters,
        settings=GoogleTTSService.Settings(**settings),
    )


INFO = ProviderInfo(
    name="Google Cloud",
    models=model_list(("chirp3-hd", "Chirp 3 HD")),
    languages=supported_languages(language_to_google_tts_language),
    credentials=["credentials_json"],
    tuning=[Tuning("speed", "Speaking speed", 0.5, 2.0, 1.0, "x", 0.05)],
)


async def voices(creds: dict[str, str]) -> list[dict]:
    from google.cloud import texttospeech_v1
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(json.loads(creds["credentials_json"]))
    client = texttospeech_v1.TextToSpeechAsyncClient(credentials=credentials)
    response = await client.list_voices()
    return [
        {"id": v.name, "name": v.name, "language": (v.language_codes or [None])[0],
         "gender": texttospeech_v1.SsmlVoiceGender(v.ssml_gender).name}
        for v in response.voices
        if "Chirp3-HD" in v.name or "Neural2" in v.name or "Wavenet" in v.name
    ]
