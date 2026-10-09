"""Sarvam chat completions (OpenAI-compatible, streaming)."""

from pipecat.services.sarvam.llm import SarvamLLMService

from app.providers.base import CallContext, ProviderInfo, Tuning, model_list, resolve_model, settings_overrides

DEFAULT_MODEL = "sarvam-105b"
ALIASES = {"sarvam-m": DEFAULT_MODEL}


def build(choice, ctx: CallContext) -> SarvamLLMService:
    model = resolve_model(choice.model, DEFAULT_MODEL, ALIASES)
    settings = {"model": model, "temperature": choice.temperature}
    if model in SarvamLLMService._REASONING_MODELS:
        # Thinking time is dead air on a call. Reasoning tokens also count
        # against max_tokens, so no cap here or replies get cut off.
        settings["reasoning_effort"] = "low"
    else:
        settings["max_tokens"] = choice.max_tokens or ctx.settings.llm_max_tokens
    settings.update(settings_overrides(SarvamLLMService.Settings, choice.options))
    return SarvamLLMService(
        api_key=ctx.secret("llm", "api_key", ctx.settings.sarvam_api_key),
        settings=SarvamLLMService.Settings(**settings),
    )

INFO = ProviderInfo(
    name="Sarvam AI",
    models=model_list((DEFAULT_MODEL, "Sarvam 105B"), ("sarvam-105b-conversations", "Sarvam 105B conversations")),
    languages=["*"],
    credentials=["api_key"],
    tuning=[Tuning("temperature", "Temperature", 0, 1, 0.35, step=0.05)],
)
