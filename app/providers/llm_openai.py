"""OpenAI chat completions (streaming)."""

from pipecat.services.openai.llm import OpenAILLMService

from app.providers.base import CallContext, resolve_model, settings_overrides

DEFAULT_MODEL = "gpt-4o-mini"


def build(choice, ctx: CallContext) -> OpenAILLMService:
    settings = {
        "model": resolve_model(choice.model, DEFAULT_MODEL, {}),
        "temperature": choice.temperature,
        "max_completion_tokens": choice.max_tokens or ctx.settings.llm_max_tokens,
    }
    settings.update(settings_overrides(OpenAILLMService.Settings, choice.options))
    return OpenAILLMService(
        api_key=ctx.settings.openai_api_key,
        # "priority" buys lower, steadier time-to-first-token where enabled.
        service_tier=choice.options.get("service_tier"),
        settings=OpenAILLMService.Settings(**settings),
    )
