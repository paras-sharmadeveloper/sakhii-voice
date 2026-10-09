"""Google Gemini on Vertex AI (default region asia-south1, Mumbai). Thinking off."""

import hashlib
import json

from google.oauth2 import service_account
from pipecat.services.google.llm import GoogleThinkingConfig
from pipecat.services.google.vertex.llm import GoogleVertexLLMService

from app.providers.base import CallContext, ProviderInfo, Tuning, model_list, resolve_model, settings_overrides

DEFAULT_MODEL = "gemini-2.5-flash"


_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_credentials_cache: dict[str, service_account.Credentials] = {}


class _VertexLLM(GoogleVertexLLMService):
    """Pipecat refreshes the token with a blocking HTTP call in __init__, i.e. on
    the event loop at every call start. One Credentials object per service
    account instead; google-genai refreshes it in a thread when it expires."""

    @staticmethod
    def _get_credentials(credentials: str | None, credentials_path: str | None):
        if not credentials and not credentials_path:
            return GoogleVertexLLMService._get_credentials(credentials, credentials_path)  # ADC
        cache_key = hashlib.sha256((credentials or credentials_path).encode()).hexdigest()
        if cache_key not in _credentials_cache:
            _credentials_cache[cache_key] = (
                service_account.Credentials.from_service_account_info(json.loads(credentials), scopes=_SCOPES)
                if credentials
                else service_account.Credentials.from_service_account_file(credentials_path, scopes=_SCOPES)
            )
        return _credentials_cache[cache_key]


def _no_thinking(model: str) -> GoogleThinkingConfig | None:
    if model.startswith("gemini-2.5"):
        return GoogleThinkingConfig(thinking_budget=0)
    if model.startswith("gemini-3") and "flash" in model:
        return GoogleThinkingConfig(thinking_level="minimal")
    return None  # Pro models can't turn thinking off


def build(choice, ctx: CallContext) -> GoogleVertexLLMService:
    model = resolve_model(choice.model, DEFAULT_MODEL, {})
    credentials = ctx.secret("llm", "credentials_json", ctx.settings.google_credentials_json)
    project = ctx.secret("llm", "project_id", ctx.settings.google_project_id)
    if not project and credentials:
        project = json.loads(credentials).get("project_id", "")
    settings = {
        "model": model,
        "temperature": choice.temperature,
        "max_tokens": choice.max_tokens or ctx.settings.llm_max_tokens,
        "thinking": _no_thinking(model),
    }
    if choice.options.get("thinking"):
        settings["thinking"] = GoogleThinkingConfig(**choice.options["thinking"])
    settings.update(settings_overrides(GoogleVertexLLMService.Settings, {k: v for k, v in choice.options.items() if k != "thinking"}))
    return _VertexLLM(
        credentials=credentials or None,
        credentials_path=ctx.settings.google_credentials_path or None,
        project_id=project,
        location=ctx.secret("llm", "location", choice.options.get("location", ctx.settings.google_location)),
        settings=GoogleVertexLLMService.Settings(**settings),
    )


INFO = ProviderInfo(
    name="Google Gemini (Vertex AI)",
    models=model_list((DEFAULT_MODEL, "Gemini 2.5 Flash"), ("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite")),
    languages=["*"],
    credentials=["credentials_json", "project_id", "location"],
    tuning=[Tuning("temperature", "Temperature", 0, 2, 0.35, step=0.05)],
    notes="Region from the credential's location (default asia-south1). Thinking is off unless options.thinking is set.",
)
