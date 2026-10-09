"""Every registered provider builds for an 8 kHz call with dummy keys, and the
catalogue metadata each one declares is sane."""

import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import providers
from app.agent_config import AgentConfig
from app.providers.base import CallContext, ProviderInfo
from app.settings import Settings


def _google_json() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return json.dumps({
        "type": "service_account", "project_id": "sakhii-test", "private_key_id": "x",
        "private_key": key.decode(), "client_email": "engine@sakhii-test.iam.gserviceaccount.com",
        "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token",
    })


GOOGLE_JSON = _google_json()
KEYS = Settings(
    sarvam_api_key="k", elevenlabs_api_key="k", openai_api_key="k", deepgram_api_key="k",
    gladia_api_key="k", assemblyai_api_key="k", groq_api_key="k", anthropic_api_key="k",
    cartesia_api_key="k", azure_speech_key="k", azure_openai_endpoint="https://x.openai.azure.com",
    azure_openai_api_key="k", google_credentials_json=GOOGLE_JSON,
)
EXTRA = {"cartesia": {"voice": "a0e99841-438c-4a64-b679-ae501e7d6091"}, "azure_openai": {"model": "my-gpt-4o-mini"}}
ALL = [(kind, name) for kind, names in providers.REGISTRY.items() for name in names]


def _ctx(agent_dict, kind, name, credentials=None):
    agent_dict["models"][kind] = {"provider": name, **EXTRA.get(name, {})}
    agent = AgentConfig.model_validate(agent_dict)
    return CallContext(agent=agent, settings=KEYS, sample_rate=8000, credentials=credentials or {})


def test_registry_has_the_requested_providers():
    assert set(providers.STT) == {"sarvam", "elevenlabs", "deepgram", "google", "azure", "gladia", "assemblyai"}
    assert set(providers.LLM) == {"openai", "sarvam", "gemini", "azure_openai", "groq", "anthropic"}
    assert set(providers.TTS) == {"sarvam", "elevenlabs", "azure", "google", "cartesia", "deepgram"}


@pytest.mark.parametrize(("kind", "name"), ALL)
async def test_builds_at_8khz(agent_dict, kind, name):
    # async: Google's gRPC clients need a running loop, as they have during a call.
    c = _ctx(agent_dict, kind, name)
    service = providers.build(kind, getattr(c.agent.models, kind), c)
    if kind in ("stt", "tts"):
        assert service._init_sample_rate == 8000


@pytest.mark.parametrize(("kind", "name"), ALL)
def test_declares_catalogue_info(kind, name):
    import importlib

    module = importlib.import_module(providers.REGISTRY[kind][name].partition(":")[0])
    info: ProviderInfo = module.INFO
    d = info.to_dict()
    assert d["name"] and d["credentials"] and d["languages"]
    assert d["models"] or name == "azure_openai"  # Azure: the deployment name is the model
    assert json.dumps(d)  # serialisable for /catalog
    for t in d["tuning"]:
        assert t["min"] <= t["default"] <= t["max"]


def test_gemini_runs_in_mumbai_with_thinking_off(agent_dict):
    c = _ctx(agent_dict, "llm", "gemini")
    llm = providers.build("llm", c.agent.models.llm, c)
    assert llm._location == "asia-south1"
    assert llm._project_id == "sakhii-test"  # from the service-account JSON
    assert llm._settings.thinking.thinking_budget == 0
    # Same service account → same Credentials object, no token fetch per call.
    again = providers.build("llm", c.agent.models.llm, c)
    assert again._credentials is llm._credentials and not llm._credentials.token


def test_azure_v1_endpoint_needs_no_api_version(agent_dict):
    c = _ctx(agent_dict, "llm", "azure_openai",
             credentials={"llm": {"endpoint": "https://x.openai.azure.com/openai/v1", "api_key": "k"}})
    llm = providers.build("llm", c.agent.models.llm, c)
    assert llm._settings.model == "my-gpt-4o-mini"


def test_call_credentials_win_over_env(agent_dict):
    c = _ctx(agent_dict, "stt", "deepgram", credentials={"stt": {"api_key": "from-laravel"}})
    assert c.secret("stt", "api_key", KEYS.deepgram_api_key) == "from-laravel"
    assert c.secret("tts", "api_key", "env") == "env"


def test_languages_come_from_the_service_maps():
    from app.providers import stt_assemblyai, stt_google, tts_deepgram

    assert "or-IN" not in stt_google.INFO.languages
    assert {"hi-IN", "en-IN"} <= set(stt_google.INFO.languages)
    assert set(stt_assemblyai.INFO.languages) <= {"hi-IN", "en-IN", "mr-IN"}
    assert all(lang.startswith("en") for lang in tts_deepgram.INFO.languages)
