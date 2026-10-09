"""Azure Speech streaming STT (use an India region, e.g. centralindia)."""

from pipecat.services.azure.common import language_to_azure_language
from pipecat.services.azure.stt import AzureSTTService

from app.providers.base import (
    CallContext,
    ProviderInfo,
    language_enum,
    model_list,
    settings_overrides,
    supported_languages,
)


def build(choice, ctx: CallContext) -> AzureSTTService:
    settings = {"language": language_enum(ctx.language)}
    settings.update(settings_overrides(AzureSTTService.Settings, choice.options))
    return AzureSTTService(
        api_key=ctx.secret("stt", "api_key", ctx.settings.azure_speech_key),
        region=ctx.secret("stt", "region", ctx.settings.azure_speech_region),
        sample_rate=ctx.sample_rate,
        settings=AzureSTTService.Settings(**settings),
    )


INFO = ProviderInfo(
    name="Azure Speech",
    models=model_list(("default", "Azure Speech (real-time)")),
    languages=supported_languages(language_to_azure_language),
    credentials=["api_key", "region"],
)
