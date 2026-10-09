"""AssemblyAI streaming STT. Our VAD forces the end of each turn."""

from pipecat.services.assemblyai.stt import AssemblyAISTTService, language_to_assemblyai_language

from app.providers.base import (
    CallContext,
    ProviderInfo,
    language_enum,
    model_list,
    resolve_model,
    settings_overrides,
    supported_languages,
)

DEFAULT_MODEL = "universal-3-5-pro"


def build(choice, ctx: CallContext) -> AssemblyAISTTService:
    settings = {"model": resolve_model(choice.model, DEFAULT_MODEL, {}), "language": language_enum(ctx.language)}
    settings.update(settings_overrides(AssemblyAISTTService.Settings, choice.options))
    return AssemblyAISTTService(
        api_key=ctx.secret("stt", "api_key", ctx.settings.assemblyai_api_key),
        sample_rate=ctx.sample_rate,
        vad_force_turn_endpoint=True,
        settings=AssemblyAISTTService.Settings(**settings),
    )


INFO = ProviderInfo(
    name="AssemblyAI",
    models=model_list((DEFAULT_MODEL, "Universal streaming")),
    languages=supported_languages(language_to_assemblyai_language),
    credentials=["api_key"],
)
