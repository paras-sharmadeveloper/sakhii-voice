"""OpenAI chat completions (streaming)."""

from pipecat.services.openai.llm import OpenAILLMService

from app.providers.base import CallContext, ProviderInfo, Tuning, model_list, resolve_model, settings_overrides

DEFAULT_MODEL = "gpt-4o-mini"


def build(choice, ctx: CallContext) -> OpenAILLMService:
    settings = {
        "model": resolve_model(choice.model, DEFAULT_MODEL, {}),
        "temperature": choice.temperature,
        "max_completion_tokens": choice.max_tokens or ctx.settings.llm_max_tokens,
    }
    settings.update(settings_overrides(OpenAILLMService.Settings, choice.options))
    return OpenAILLMService(
        api_key=ctx.secret("llm", "api_key", ctx.settings.openai_api_key),
        # "priority" buys lower, steadier time-to-first-token where enabled.
        service_tier=choice.options.get("service_tier"),
        settings=OpenAILLMService.Settings(**settings),
    )

INFO = ProviderInfo(
    name="OpenAI",
    models=model_list(("gpt-4o-mini", "GPT-4o mini"), ("gpt-4.1-mini", "GPT-4.1 mini"), ("gpt-4.1", "GPT-4.1")),
    languages=["*"],
    credentials=["api_key"],
    tuning=[Tuning("temperature", "Temperature", 0, 1, 0.35, step=0.05)],
)
