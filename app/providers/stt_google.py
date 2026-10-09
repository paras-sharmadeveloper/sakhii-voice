"""Google Cloud Speech-to-Text v2 streaming."""

from pipecat.services.google.stt import GoogleSTTService, language_to_google_stt_language

from app.providers.base import (
    CallContext,
    ProviderInfo,
    language_enum,
    model_list,
    resolve_model,
    settings_overrides,
    supported_languages,
)

DEFAULT_MODEL = "latest_long"


def build(choice, ctx: CallContext) -> GoogleSTTService:
    codes = [ctx.language, *ctx.agent.languages.also] if ctx.multilingual else [ctx.language]
    languages = [lang for lang in (language_enum(c) for c in codes) if lang]
    settings = {"model": resolve_model(choice.model, DEFAULT_MODEL, {}), "languages": languages}
    settings.update(settings_overrides(GoogleSTTService.Settings, choice.options))
    return GoogleSTTService(
        credentials=ctx.secret("stt", "credentials_json", ctx.settings.google_credentials_json) or None,
        credentials_path=ctx.settings.google_credentials_path or None,
        location=ctx.secret("stt", "location", choice.options.get("location", "global")),
        sample_rate=ctx.sample_rate,
        settings=GoogleSTTService.Settings(**settings),
    )


INFO = ProviderInfo(
    name="Google Cloud",
    models=model_list((DEFAULT_MODEL, "Latest long"), ("telephony", "Telephony"), ("chirp_2", "Chirp 2")),
    languages=supported_languages(language_to_google_stt_language),
    credentials=["credentials_json", "location"],
)
