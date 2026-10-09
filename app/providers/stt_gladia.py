"""Gladia streaming STT (Solaria), with code-switching across the agent's languages."""

from pipecat.services.gladia.config import LanguageConfig
from pipecat.services.gladia.stt import GladiaSTTService, language_to_gladia_language

from app.providers.base import (
    CallContext,
    ProviderInfo,
    model_list,
    resolve_model,
    settings_overrides,
    supported_languages,
)

DEFAULT_MODEL = "solaria-1"


def build(choice, ctx: CallContext) -> GladiaSTTService:
    codes = [ctx.language, *ctx.agent.languages.also]
    languages = list(dict.fromkeys(c.split("-")[0] for c in codes))
    settings = {
        "model": resolve_model(choice.model, DEFAULT_MODEL, {}),
        "language_config": LanguageConfig(languages=languages, code_switching=ctx.agent.languages.code_switching),
    }
    settings.update(settings_overrides(GladiaSTTService.Settings, choice.options))
    return GladiaSTTService(
        api_key=ctx.secret("stt", "api_key", ctx.settings.gladia_api_key),
        sample_rate=ctx.sample_rate,
        settings=GladiaSTTService.Settings(**settings),
    )


INFO = ProviderInfo(
    name="Gladia",
    models=model_list((DEFAULT_MODEL, "Solaria")),
    languages=supported_languages(language_to_gladia_language),
    credentials=["api_key"],
)
