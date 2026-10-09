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
    # Decrypted credential fields per kind ("stt"/"llm"/"tts"), see app/credentials.py.
    credentials: dict[str, dict[str, str]] = field(default_factory=dict)

    def secret(self, kind: str, name: str, fallback: str = "") -> str:
        """A credential field for this call, or the .env fallback."""
        return self.credentials.get(kind, {}).get(name) or fallback

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


# ── Catalogue metadata (GET /catalog) ─────────────────────────────────────────
#
# Each provider module declares INFO; modules with a voices API also define
#     async def voices(creds: dict[str, str]) -> list[dict]
# (creds = credential fields, falling back to the engine's .env keys).

INDIAN_LANGUAGES = ["hi-IN", "en-IN", "bn-IN", "gu-IN", "kn-IN", "ml-IN", "mr-IN", "or-IN", "pa-IN", "ta-IN", "te-IN"]


@dataclass
class Tuning:
    key: str
    label: str
    min: float
    max: float
    default: float
    unit: str = ""
    step: float = 1


@dataclass
class ProviderInfo:
    name: str
    models: list[dict[str, str]]
    languages: list[str]
    credentials: list[str]
    streaming: bool = True
    tuning: list[Tuning] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


def supported_languages(convert, candidates: list[str] = INDIAN_LANGUAGES) -> list[str]:
    """Codes from `candidates` that a service's Pipecat language map really has.

    Pipecat's resolve_language falls back to a guessed code for anything it
    doesn't know, so calling the converter can't tell "supported" from
    "guessed". Instead, look at the map the converter passes it: a language
    counts if the map has it, or its base language (hi-IN → hi).
    """
    namespace = convert.__globals__
    original = namespace.get("resolve_language")
    if original is None:
        return []
    found: list[str] = []

    def spy(language, language_map, use_base_code=True):
        base = str(language.value).split("-")[0]
        try:
            base_language = Language(base)
        except ValueError:
            base_language = None
        hit = language_map.get(language) or (language_map.get(base_language) if base_language else None)
        if hit:
            found.append(str(language.value))
        return hit or ""

    namespace["resolve_language"] = spy
    try:
        for code in candidates:
            try:
                convert(Language(code))
            except Exception:
                continue
    finally:
        namespace["resolve_language"] = original
    return [c for c in candidates if c in found]


def model_list(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"id": i, "name": n} for i, n in pairs]
