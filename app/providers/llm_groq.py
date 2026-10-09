"""Groq (OpenAI-compatible, very low latency)."""

from pipecat.services.groq.llm import GroqLLMService

from app.providers.base import CallContext, ProviderInfo, Tuning, model_list, resolve_model, settings_overrides

DEFAULT_MODEL = "llama-3.3-70b-versatile"


def build(choice, ctx: CallContext) -> GroqLLMService:
    settings = {
        "model": resolve_model(choice.model, DEFAULT_MODEL, {}),
        "temperature": choice.temperature,
        "max_completion_tokens": choice.max_tokens or ctx.settings.llm_max_tokens,
    }
    settings.update(settings_overrides(GroqLLMService.Settings, choice.options))
    return GroqLLMService(
        api_key=ctx.secret("llm", "api_key", ctx.settings.groq_api_key),
        settings=GroqLLMService.Settings(**settings),
    )


INFO = ProviderInfo(
    name="Groq",
    models=model_list((DEFAULT_MODEL, "Llama 3.3 70B"), ("openai/gpt-oss-120b", "GPT-OSS 120B"), ("llama-3.1-8b-instant", "Llama 3.1 8B")),
    languages=["*"],
    credentials=["api_key"],
    tuning=[Tuning("temperature", "Temperature", 0, 2, 0.35, step=0.05)],
)
