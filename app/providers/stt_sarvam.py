"""Sarvam streaming STT (saaras realtime websocket)."""

from pipecat.services.sarvam.stt import SarvamRealtimeSTTService, language_to_sarvam_realtime_language

from app.providers.base import (
    CallContext,
    ProviderInfo,
    model_list,
    resolve_model,
    settings_overrides,
    supported_languages,
)

DEFAULT_MODEL = "saaras:v3-realtime"
# Batch-only / retired ids -> streaming model.
ALIASES = {
    "saarika:v2.5": DEFAULT_MODEL,
    "saarika:v2": DEFAULT_MODEL,
    "saaras:v3": DEFAULT_MODEL,
    "saaras:v4-realtime": "saaras:v4",
}


def build(choice, ctx: CallContext) -> SarvamRealtimeSTTService:
    langs = ctx.agent.languages
    settings = {
        "model": resolve_model(choice.model, DEFAULT_MODEL, ALIASES),
        "language_code": "auto" if ctx.multilingual else ctx.language,
        # codemix keeps Hinglish as Hindi words + English words instead of
        # forcing everything into one script; the LLM reads it better.
        "mode": "codemix" if langs.code_switching else "transcribe",
        "stream_type": "fast",
    }
    settings.update(settings_overrides(SarvamRealtimeSTTService.Settings, choice.options))
    return SarvamRealtimeSTTService(
        api_key=ctx.secret("stt", "api_key", ctx.settings.sarvam_api_key),
        # Our VAD + turn model decide when the caller is done, the same way
        # for every STT provider; Sarvam finalises on our speech_end.
        endpointing="manual",
        sample_rate=ctx.sample_rate,
        settings=SarvamRealtimeSTTService.Settings(**settings),
    )

INFO = ProviderInfo(
    name="Sarvam AI",
    models=model_list((DEFAULT_MODEL, "Saaras v3 realtime"), ("saaras:v4", "Saaras v4")),
    languages=supported_languages(language_to_sarvam_realtime_language),
    credentials=["api_key"],
)
