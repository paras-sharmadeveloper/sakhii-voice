from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.sarvam.llm import SarvamLLMService
from pipecat.services.sarvam.stt import SarvamRealtimeSTTService
from pipecat.services.sarvam.tts import SarvamTTSService

from app import providers
from app.agent_config import AgentConfig
from app.providers.base import CallContext
from app.settings import Settings

KEYS = Settings(sarvam_api_key="k", elevenlabs_api_key="k", openai_api_key="k")


def ctx(agent_dict):
    return CallContext(agent=AgentConfig.model_validate(agent_dict), settings=KEYS, sample_rate=8000)


def test_every_registered_provider_imports():
    providers.preload()


def test_builds_sarvam_stack_with_legacy_ids_mapped(agent_dict):
    c = ctx(agent_dict)
    stt = providers.build("stt", c.agent.models.stt, c)
    tts = providers.build("tts", c.agent.models.tts, c)
    assert isinstance(stt, SarvamRealtimeSTTService)
    assert stt._settings.model == "saaras:v3-realtime"
    assert stt._settings.language_code == "auto"  # Hindi + English, auto-detect on
    assert isinstance(tts, SarvamTTSService)
    assert tts._settings.model == "bulbul:v3"
    assert tts._settings.voice == "priya"  # bulbul:v2 "Anushka" mapped


def test_builds_elevenlabs_and_llms(agent_dict):
    agent_dict["models"] = {
        "stt": {"provider": "elevenlabs", "model": "scribe_v1"},
        "llm": {"provider": "sarvam", "model": "sarvam-m"},
        "tts": {
            "provider": "elevenlabs",
            "voice": "21m00Tcm4TlvDq8ikWAM",
            "options": {"stability": 70, "similarity": 75, "style": 30},
        },
    }
    c = ctx(agent_dict)
    stt = providers.build("stt", c.agent.models.stt, c)
    llm = providers.build("llm", c.agent.models.llm, c)
    tts = providers.build("tts", c.agent.models.tts, c)
    assert isinstance(stt, ElevenLabsRealtimeSTTService) and stt._settings.model == "scribe_v2_realtime"
    assert isinstance(llm, SarvamLLMService) and llm._settings.reasoning_effort == "low"
    assert isinstance(tts, ElevenLabsTTSService)
    assert tts._settings.stability == 0.7 and tts._settings.similarity_boost == 0.75

    agent_dict["models"]["llm"] = {"provider": "openai", "model": "gpt-4.1-mini", "temperature": 0.2}
    c = ctx(agent_dict)
    llm = providers.build("llm", c.agent.models.llm, c)
    assert isinstance(llm, OpenAILLMService) and llm._settings.temperature == 0.2


def test_unknown_provider(agent_dict):
    agent_dict["models"]["llm"]["provider"] = "no-such-llm"
    c = ctx(agent_dict)
    try:
        providers.build("llm", c.agent.models.llm, c)
    except providers.UnknownProvider:
        return
    raise AssertionError("expected UnknownProvider")
