"""Deepgram streaming STT (Nova). Finalizes on our VAD's end of speech."""

from pipecat.services.deepgram.stt import DeepgramSTTService

from app.providers.base import CallContext, ProviderInfo, model_list, resolve_model, settings_overrides

DEFAULT_MODEL = "nova-3-general"


def _language(ctx: CallContext) -> str:
    if ctx.multilingual:
        return "multi"  # Nova-3 multilingual (code-switching)
    return "en-IN" if ctx.language == "en-IN" else ctx.language.split("-")[0]


def build(choice, ctx: CallContext) -> DeepgramSTTService:
    settings = {"model": resolve_model(choice.model, DEFAULT_MODEL, {}), "language": _language(ctx)}
    settings.update(settings_overrides(DeepgramSTTService.Settings, choice.options))
    return DeepgramSTTService(
        api_key=ctx.secret("stt", "api_key", ctx.settings.deepgram_api_key),
        sample_rate=ctx.sample_rate,
        settings=DeepgramSTTService.Settings(**settings),
    )


INFO = ProviderInfo(
    name="Deepgram",
    models=model_list((DEFAULT_MODEL, "Nova-3"), ("nova-2-general", "Nova-2")),
    # Pipecat has no language map for Deepgram (codes pass through); per
    # Deepgram's docs Nova-3 covers Hindi and Indian English ("multi" for both).
    languages=["hi-IN", "en-IN"],
    credentials=["api_key"],
)
