"""What every provider module gets, and small helpers they share.

A provider module is one file exposing:

    def build(choice, ctx: CallContext) -> <Pipecat service>

where `choice` is the agent's ModelChoice / LLMChoice / TTSChoice for that
slot. Register it with one line in app/providers/__init__.py.
"""

from dataclasses import dataclass, field, fields
from typing import Any

from loguru import logger
from pipecat.transcriptions.language import Language
from pipecat.utils.text.base_text_filter import BaseTextFilter

from app.agent_config import AgentConfig
from app.settings import Settings


@dataclass
class CallContext:
    agent: AgentConfig
    settings: Settings
    # Telephony audio rate. Services are asked for exactly this rate so no
    # resampling happens anywhere in the pipeline.
    sample_rate: int
    # Applied by TTS services just before synthesis (pronunciation rules).
    text_filters: list[BaseTextFilter] = field(default_factory=list)

    @property
    def language(self) -> str:
        return self.agent.languages.primary

    @property
    def multilingual(self) -> bool:
        langs = self.agent.languages
        return langs.auto_detect and bool(set(langs.also) - {langs.primary})


def language_enum(code: str) -> Language | None:
    try:
        return Language(code)
    except ValueError:
        logger.warning("Unknown language code {!r}, letting the provider decide", code)
        return None


def resolve_model(requested: str | None, default: str, aliases: dict[str, str]) -> str:
    """Model id from config, with retired/batch-only ids mapped to their
    streaming successors so an old builder value never costs latency."""
    if not requested:
        return default
    if requested in aliases:
        logger.info("Model {} mapped to {}", requested, aliases[requested])
        return aliases[requested]
    return requested


def settings_overrides(settings_cls: type, options: dict[str, Any]) -> dict[str, Any]:
    """The subset of an agent's provider `options` that the service's Settings
    dataclass actually accepts. Unknown keys are ignored, not fatal."""
    allowed = {f.name for f in fields(settings_cls) if not f.name.startswith("_")}
    return {k: v for k, v in options.items() if k in allowed}


def unit_interval(value: Any) -> float:
    """Builder sliders send 0-100; providers want 0-1."""
    v = float(value)
    return v / 100.0 if v > 1.0 else v
