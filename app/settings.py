"""Process-wide settings, read once from the environment / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8800
    log_level: str = "INFO"

    exotel_ws_token: str = ""
    exotel_account_sid: str = ""
    exotel_sample_rate: int = 8000

    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_key_prefix: str = ""
    call_state_ttl_secs: int = 86_400
    events_stream_maxlen: int = 100_000

    sarvam_api_key: str = ""
    elevenlabs_api_key: str = ""
    openai_api_key: str = ""

    # Silence after speech before the turn is evaluated (end-of-turn trigger).
    vad_stop_secs: float = 0.2
    # Speech needed before the VAD reports "caller started speaking".
    vad_start_secs: float = 0.2
    vad_confidence: float = 0.7
    # smart = Smart Turn model judges each pause; off = fixed silence timeout.
    turn_detection: str = "smart"
    # Smart turn on: max silence when the model thinks the caller isn't done.
    smart_turn_stop_secs: float = 3.0
    # Smart turn off: silence that ends a turn.
    user_speech_timeout: float = 0.6

    # Barge-in while the bot is speaking: continuous speech required, or that
    # many transcribed words, before the bot is interrupted.
    interrupt_min_speech_secs: float = 0.4
    interrupt_min_words: int = 3

    llm_max_tokens: int = 220
    default_max_call_secs: int = 600


@lru_cache
def get_settings() -> Settings:
    return Settings()
