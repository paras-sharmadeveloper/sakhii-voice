"""Agent config as Laravel writes it to Redis (schema_version 1).

Field names follow the Agent Builder tabs (Identity, Models, Persona, Speak,
Knowledge, Integrations). Everything is optional except the three model
choices, so Laravel can grow the payload without breaking running engines.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


# The builder shows language names; Redis may carry either names or codes.
LANGUAGE_CODES = {
    "hindi": "hi-IN",
    "english": "en-IN",
    "bengali": "bn-IN",
    "gujarati": "gu-IN",
    "kannada": "kn-IN",
    "malayalam": "ml-IN",
    "marathi": "mr-IN",
    "odia": "or-IN",
    "punjabi": "pa-IN",
    "tamil": "ta-IN",
    "telugu": "te-IN",
}
LANGUAGE_NAMES = {code: name.title() for name, code in LANGUAGE_CODES.items()}


def to_language_code(value: str) -> str:
    return LANGUAGE_CODES.get(value.strip().lower(), value.strip())


class ModelChoice(_Model):
    provider: str
    model: str | None = None
    # Provider-specific knobs (voice tuning sliders, reasoning effort, ...).
    options: dict[str, Any] = Field(default_factory=dict)
    # sakhii:voice:cred:<id> (app/credentials.py); None = the engine's .env keys.
    credential_id: str | None = None


class LLMChoice(ModelChoice):
    temperature: float = 0.35
    max_tokens: int | None = None


class TTSChoice(ModelChoice):
    voice: str | None = None
    # Builder "Speaking speed": 100% == 1.0
    speed: float = 1.0


class Models(_Model):
    stt: ModelChoice
    llm: LLMChoice
    tts: TTSChoice


class Greeting(_Model):
    opening: str = ""
    closing: str = ""


class Business(_Model):
    name: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    address: str = ""
    description: str = ""


class Transfer(_Model):
    enabled: bool = False
    number: str | None = None
    # Said right before handing over. Empty = a default in the primary language.
    message: str = ""


class Languages(_Model):
    primary: str = "hi-IN"
    also: list[str] = Field(default_factory=list)
    code_switching: bool = True
    auto_detect: bool = True

    @field_validator("primary")
    @classmethod
    def _primary(cls, v: str) -> str:
        return to_language_code(v)

    @field_validator("also")
    @classmethod
    def _also(cls, v: list[str]) -> list[str]:
        return [to_language_code(x) for x in v]


class Pronunciation(_Model):
    term: str
    say: str
    language: str = "all"


class Filler(_Model):
    text: str
    language: str = ""
    context: str = ""


class Persona(_Model):
    base_tone: str = "Professional and empathetic"
    emotion_awareness: str = "Adaptive"


class FAQ(_Model):
    question: str
    answer: str


class Knowledge(_Model):
    faqs: list[FAQ] = Field(default_factory=list)
    # Small knowledge bases ride in the prompt (no retrieval round trip).
    inline_text: str = ""
    # Larger ones: Laravel exposes a search endpoint; the agent calls it as a
    # tool only when the caller asks something the documents cover.
    search_url: str | None = None


class HttpTool(_Model):
    """An Integrations-tab tool: the LLM calls it, we POST/GET the URL."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}, "required": []}
    )
    url: str
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_secs: float = 8.0
    # Line the agent says while the tool runs, e.g. "Ek minute, check karti hoon".
    wait_message: str | None = None


class AnalysisField(_Model):
    key: str
    description: str = ""


DEFAULT_OUTCOMES = ["resolved", "follow_up_needed", "transferred", "not_interested", "no_conversation"]


class Analysis(_Model):
    """After-call summary (call_analyzed event)."""

    enabled: bool = True
    outcomes: list[str] = Field(default_factory=lambda: list(DEFAULT_OUTCOMES))
    # Values to pull out of the conversation, e.g. {"key": "promised_payment_date"}.
    fields: list[AnalysisField] = Field(default_factory=list)


class AgentConfig(_Model):
    schema_version: int = 1
    agent_id: str
    tenant_id: str | None = None
    name: str = ""
    display_name: str = "Sakhii"
    use_case: str = ""

    greeting: Greeting = Field(default_factory=Greeting)
    business: Business = Field(default_factory=Business)
    transfer: Transfer = Field(default_factory=Transfer)
    max_call_duration_secs: int | None = None

    models: Models
    system_prompt: str = ""
    persona: Persona = Field(default_factory=Persona)

    languages: Languages = Field(default_factory=Languages)
    language_overrides: dict[str, str] = Field(default_factory=dict)
    fillers: list[Filler] = Field(default_factory=list)
    pronunciations: list[Pronunciation] = Field(default_factory=list)

    knowledge: Knowledge = Field(default_factory=Knowledge)
    tools: list[HttpTool] = Field(default_factory=list)

    # Default values for {placeholders}; per-call values override these.
    variables: dict[str, str] = Field(default_factory=dict)

    # Record calls (stereo: caller left, agent right) to S3-compatible storage.
    recording_enabled: bool = False
    analysis: Analysis = Field(default_factory=Analysis)

    @field_validator("agent_id", "tenant_id", mode="before")
    @classmethod
    def _stringify_ids(cls, v: Any) -> Any:
        return None if v is None else str(v)

    @field_validator("language_overrides")
    @classmethod
    def _override_keys(cls, v: dict[str, str]) -> dict[str, str]:
        return {to_language_code(k): text for k, text in v.items() if text}
