"""Anthropic Claude."""

from pipecat.services.anthropic.llm import AnthropicLLMService

from app.providers.base import CallContext, ProviderInfo, Tuning, model_list, resolve_model, settings_overrides

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def build(choice, ctx: CallContext) -> AnthropicLLMService:
    settings = {
        "model": resolve_model(choice.model, DEFAULT_MODEL, {}),
        "temperature": choice.temperature,
        "max_tokens": choice.max_tokens or ctx.settings.llm_max_tokens,
        # The system prompt is the same every turn: cache it.
        "enable_prompt_caching": True,
    }
    settings.update(settings_overrides(AnthropicLLMService.Settings, choice.options))
    return AnthropicLLMService(
        api_key=ctx.secret("llm", "api_key", ctx.settings.anthropic_api_key),
        settings=AnthropicLLMService.Settings(**settings),
    )


INFO = ProviderInfo(
    name="Anthropic",
    models=model_list((DEFAULT_MODEL, "Claude Haiku 4.5 (fastest)"), ("claude-sonnet-5-5", "Claude Sonnet 5.5")),
    languages=["*"],
    credentials=["api_key"],
    tuning=[Tuning("temperature", "Temperature", 0, 1, 0.35, step=0.05)],
)
