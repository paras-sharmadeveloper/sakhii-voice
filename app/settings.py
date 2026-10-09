"""Engine settings.

Three kinds (README, "Settings"):
- bootstrap: only from the environment / .env (Redis connection, the key that
  decrypts secrets, the listen address). Changing one needs a restart.
- runtime: sakhii:voice:settings in Redis (JSON), hot-reloaded.
- secret: sakhii:voice:secrets in Redis (AES-256-GCM), hot-reloaded.

Precedence: Redis value > environment / .env > the default below, so a server
with only a .env keeps working before Laravel writes anything.

get_settings() is the current snapshot. Code running inside a call sees the
snapshot the call started with (pinned()), so a reload never changes a live call.
"""

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BOOTSTRAP = {"bootstrap": True}
SECRET = {"secret": True}


def _secret(description: str) -> Any:
    return Field("", description=description, repr=False, json_schema_extra=SECRET)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Bootstrap (environment / .env only) ---
    host: str = Field("127.0.0.1", description="Listen address.", json_schema_extra=BOOTSTRAP)
    port: int = Field(8000, ge=1, le=65535, description="Listen port.", json_schema_extra=BOOTSTRAP)
    redis_host: str = Field("", description="Redis host. Empty = use REDIS_URL.", json_schema_extra=BOOTSTRAP)
    redis_port: int = Field(6379, ge=1, le=65535, description="Redis port.", json_schema_extra=BOOTSTRAP)
    redis_password: str = Field("", repr=False, description="Redis password.", json_schema_extra=BOOTSTRAP)
    redis_db: int = Field(0, ge=0, le=15, description="Redis database number.", json_schema_extra=BOOTSTRAP)
    redis_url: str = Field(
        "redis://127.0.0.1:6379/0", repr=False,
        description="Used only when REDIS_HOST is empty (older .env files).", json_schema_extra=BOOTSTRAP,
    )
    redis_key_prefix: str = Field(
        "", description="Laravel's REDIS_PREFIX, so both sides use the same keys.", json_schema_extra=BOOTSTRAP
    )
    sakhii_voice_cred_key: str = Field(
        "", repr=False, description="base64 of 32 bytes; decrypts sakhii:voice:secrets and sakhii:voice:cred:<id>. Same value as in Laravel.",
        json_schema_extra=BOOTSTRAP,
    )

    # --- Secrets (sakhii:voice:secrets) ---
    exotel_ws_token: str = _secret("Token in the Voicebot URL (/ws/exotel/<token>). Empty = no check (local testing only).")
    engine_admin_token: str = _secret("Bearer token for GET /catalog. Empty = /catalog is off.")
    sarvam_api_key: str = _secret("Platform Sarvam key (used when an agent has no credential_id).")
    elevenlabs_api_key: str = _secret("Platform ElevenLabs key.")
    openai_api_key: str = _secret("Platform OpenAI key (also used by after-call analysis).")
    deepgram_api_key: str = _secret("Platform Deepgram key.")
    gladia_api_key: str = _secret("Platform Gladia key.")
    assemblyai_api_key: str = _secret("Platform AssemblyAI key.")
    groq_api_key: str = _secret("Platform Groq key.")
    anthropic_api_key: str = _secret("Platform Anthropic key.")
    cartesia_api_key: str = _secret("Platform Cartesia key.")
    azure_speech_key: str = _secret("Platform Azure Speech key.")
    azure_openai_api_key: str = _secret("Platform Azure OpenAI key.")
    google_credentials_json: str = _secret("Google service-account JSON (STT, TTS, Gemini on Vertex).")
    recording_s3_key: str = _secret("Recording storage access key.")
    recording_s3_secret: str = _secret("Recording storage secret key.")

    # --- Runtime (sakhii:voice:settings) ---
    log_level: Literal["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"] = Field(
        "INFO", description="Engine log level."
    )
    exotel_account_sid: str = Field("", description="If set, a stream's account_sid must match.")
    exotel_sample_rate: Literal[8000, 16000] = Field(8000, description="Exotel stream sample rate (Hz).")
    call_state_ttl_secs: int = Field(86_400, ge=3600, le=2_592_000, description="TTL of sakhii:voice:call:<CallSid>.")
    events_stream_maxlen: int = Field(100_000, ge=1000, le=10_000_000, description="Approximate cap on sakhii:voice:events.")

    azure_speech_region: str = Field("centralindia", description="Azure Speech region.")
    azure_openai_endpoint: str = Field("", description="Azure OpenAI endpoint (…/openai/v1 for the v1 API).")
    azure_openai_api_version: str = Field("2024-10-21", description="Azure OpenAI API version (non-v1 endpoints).")
    google_credentials_path: str = Field("", description="Path to a service-account JSON on the server (instead of GOOGLE_CREDENTIALS_JSON).")
    google_project_id: str = Field("", description="Vertex AI project. Empty = from the service-account JSON.")
    google_location: str = Field("asia-south1", description="Vertex AI region for Gemini.")
    default_models: dict[str, str] = Field(
        default_factory=dict,
        description='Model used when an agent leaves "model" empty, by "<kind>.<provider>", e.g. {"stt.deepgram": "nova-3-general"}. Otherwise each provider\'s built-in default.',
    )

    recording_s3_endpoint: str = Field("", description="S3-compatible endpoint. Empty = AWS S3.")
    recording_s3_bucket: str = Field("", description="Recording bucket. Empty = recording off.")
    recording_s3_region: str = Field("auto", description="Storage region (\"auto\" for R2).")
    recording_s3_prefix: str = Field("recordings/", description="Object key prefix.")
    recording_public_base_url: str = Field("", description="Public/CDN base for recording_url. Empty = private bucket, only recording_key is sent.")
    recording_mp3_kbps: int = Field(32, ge=16, le=128, description="Recording MP3 bitrate.")

    analysis_enabled: bool = Field(True, description="After-call analysis (call.analyzed) on/off for every agent.")
    analysis_model: str = Field("gpt-4o-mini", description="OpenAI model for after-call analysis.")

    vad_stop_secs: float = Field(0.2, ge=0.1, le=2.0, description="Silence after speech before the turn is evaluated. Each 0.1 s adds 0.1 s to every reply.")
    vad_start_secs: float = Field(0.2, ge=0.05, le=1.0, description="Speech needed before the caller counts as speaking.")
    vad_confidence: float = Field(0.7, ge=0.3, le=0.95, description="VAD speech confidence threshold.")
    turn_detection: Literal["smart", "off"] = Field("smart", description="smart = Smart Turn model judges each pause; off = fixed silence (USER_SPEECH_TIMEOUT).")
    smart_turn_stop_secs: float = Field(3.0, ge=0.5, le=10.0, description="Smart turn fallback: max silence when the model thinks the caller isn't finished.")
    user_speech_timeout: float = Field(0.6, ge=0.2, le=3.0, description="Turn detection off: silence that ends a turn.")
    interrupt_min_speech_secs: float = Field(0.4, ge=0.0, le=3.0, description="Barge-in: continuous caller speech needed to stop the bot.")
    interrupt_min_words: int = Field(3, ge=1, le=20, description="Barge-in: or this many transcribed words.")

    llm_max_tokens: int = Field(220, ge=32, le=2000, description="Cap on LLM reply length when the agent doesn't set one.")
    default_max_call_secs: int = Field(600, ge=30, le=7200, description="Call length limit when the agent doesn't set one.")
    drain_timeout_secs: int = Field(615, ge=0, le=7200, description="On restart, how long live calls may continue. Keep above the longest call.")

    @field_validator("log_level", "turn_detection", mode="before")
    @classmethod
    def _case_insensitive(cls, v: Any, info) -> Any:
        """Older .env files have LOG_LEVEL=info; Laravel may send "Smart"."""
        if not isinstance(v, str):
            return v
        return v.strip().upper() if info.field_name == "log_level" else v.strip().lower()

    @classmethod
    def kind(cls, name: str) -> str:
        extra = cls.model_fields[name].json_schema_extra or {}
        return "bootstrap" if extra.get("bootstrap") else "secret" if extra.get("secret") else "runtime"


def names(kind: str) -> set[str]:
    return {n for n in Settings.model_fields if Settings.kind(n) == kind}


_current: Settings | None = None
_pinned: contextvars.ContextVar[Settings | None] = contextvars.ContextVar("sakhii_settings", default=None)


def get_settings() -> Settings:
    pinned_settings = _pinned.get()
    if pinned_settings is not None:
        return pinned_settings
    global _current
    if _current is None:
        _current = Settings()
    return _current


def _reset() -> None:
    """Forget the snapshot; the next get_settings() reads the environment again."""
    global _current
    _current = None


get_settings.cache_clear = _reset  # type: ignore[attr-defined]


def apply(settings: Settings) -> None:
    """Make this the snapshot for everything that starts from now on."""
    global _current
    _current = settings


@contextmanager
def pinned(settings: Settings) -> Iterator[Settings]:
    """Everything in this block, including tasks and threads it starts, sees `settings`."""
    token = _pinned.set(settings)
    try:
        yield settings
    finally:
        _pinned.reset(token)
