"""ElevenLabs Scribe realtime STT (websocket)."""

from pipecat.services.elevenlabs.stt import (
    CommitStrategy,
    ElevenLabsRealtimeSTTService,
    language_to_elevenlabs_language,
)

from app.providers.base import (
    CallContext,
    ProviderInfo,
    language_enum,
    model_list,
    resolve_model,
    settings_overrides,
    supported_languages,
)

DEFAULT_MODEL = "scribe_v2_realtime"
# The batch Scribe models have ~2 s p99; always stream instead.
ALIASES = {"scribe_v1": DEFAULT_MODEL, "scribe_v2": DEFAULT_MODEL}


def build(choice, ctx: CallContext) -> ElevenLabsRealtimeSTTService:
    settings = {
        "model": resolve_model(choice.model, DEFAULT_MODEL, ALIASES),
        "language": None if ctx.multilingual else language_enum(ctx.language),
    }
    settings.update(settings_overrides(ElevenLabsRealtimeSTTService.Settings, choice.options))
    return ElevenLabsRealtimeSTTService(
        api_key=ctx.secret("stt", "api_key", ctx.settings.elevenlabs_api_key),
        # Commit on our VAD/turn decision, same as every other STT provider.
        commit_strategy=CommitStrategy.MANUAL,
        sample_rate=ctx.sample_rate,
        settings=ElevenLabsRealtimeSTTService.Settings(**settings),
    )

INFO = ProviderInfo(
    name="ElevenLabs",
    models=model_list((DEFAULT_MODEL, "Scribe v2 realtime")),
    languages=supported_languages(language_to_elevenlabs_language),
    credentials=["api_key"],
)
