"""Azure OpenAI (your deployment, e.g. in Central India)."""

from pipecat.services.azure.llm import AzureLLMService

from app.providers.base import CallContext, ProviderInfo, Tuning, settings_overrides


def build(choice, ctx: CallContext) -> AzureLLMService:
    deployment = ctx.secret("llm", "deployment", choice.model or "")
    settings = {
        "model": deployment,
        "temperature": choice.temperature,
        "max_completion_tokens": choice.max_tokens or ctx.settings.llm_max_tokens,
    }
    settings.update(settings_overrides(AzureLLMService.Settings, choice.options))
    endpoint = ctx.secret("llm", "endpoint", ctx.settings.azure_openai_endpoint)
    # Endpoints ending in /openai/v1 use Azure's v1 API, which takes no api_version.
    v1 = endpoint.rstrip("/").endswith("/openai/v1")
    return AzureLLMService(
        endpoint=endpoint,
        api_key=ctx.secret("llm", "api_key", ctx.settings.azure_openai_api_key),
        api_version=None if v1 else ctx.secret("llm", "api_version", ctx.settings.azure_openai_api_version) or None,
        settings=AzureLLMService.Settings(**settings),
    )


INFO = ProviderInfo(
    name="Azure OpenAI",
    models=[],  # the deployment name is the model; it comes from the credential or the agent
    languages=["*"],
    credentials=["endpoint", "api_key", "deployment", "api_version"],
    tuning=[Tuning("temperature", "Temperature", 0, 1, 0.35, step=0.05)],
    notes="model = your Azure deployment name. An endpoint ending in /openai/v1 uses the v1 API (no api_version).",
)
