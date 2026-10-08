"""scripts/seed_test_agent.py writes keys the engine's own lookup reads back.
Needs a local redis-server (db 15)."""

import asyncio
import importlib.util
from pathlib import Path

import pytest
import redis

from app import providers, store
from app.providers.base import CallContext
from app.settings import get_settings

PREFIX = "sakhii-test-"
REDIS_URL = "redis://127.0.0.1:6379/15"

_spec = importlib.util.spec_from_file_location(
    "seed_test_agent", Path(__file__).parent.parent / "scripts" / "seed_test_agent.py"
)
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


@pytest.fixture
def env(monkeypatch, tmp_path):
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        r.ping()
    except redis.ConnectionError:
        pytest.skip("no local redis-server")
    r.flushdb()
    # Same route as on the server: settings come from an env file.
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"REDIS_URL={REDIS_URL}\nREDIS_KEY_PREFIX={PREFIX}\n"
        "SARVAM_API_KEY=k\nOPENAI_API_KEY=k\nELEVENLABS_API_KEY=k\n"
    )
    for name in ("REDIS_URL", "REDIS_KEY_PREFIX", "SARVAM_API_KEY", "OPENAI_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield r, ["--env-file", str(env_file)]
    r.flushdb()
    r.close()
    get_settings.cache_clear()


def _resolve(to_number: str):
    async def go():
        try:
            return await store.resolve_agent("CA-test", to_number, "09999999999", None)
        finally:
            await store.close()

    return asyncio.run(go())


@pytest.mark.parametrize("preset", ["sarvam", "elevenlabs"])
def test_seeded_agent_is_found_by_engine_lookup(env, preset, capsys):
    r, env_args = env
    args = ["--exophone", "08047112233", "--provider-preset", preset, *env_args]
    if preset == "elevenlabs":
        args += ["--voice-id", "21m00Tcm4TlvDq8ikWAM"]
    assert seed.main(args) == 0

    out = capsys.readouterr().out
    assert f"{PREFIX}sakhii:voice:agent:test-agent" in out
    assert f"{PREFIX}sakhii:voice:number:+918047112233 = test-agent" in out
    assert "Call the ExoPhone: 08047112233" in out
    assert set(r.keys("*")) == {
        f"{PREFIX}sakhii:voice:agent:test-agent",
        f"{PREFIX}sakhii:voice:number:+918047112233",
    }

    # Exotel may report the ExoPhone in any of its formats.
    for dialled in ("08047112233", "+918047112233", "918047112233"):
        agent = _resolve(dialled).agent
        assert agent.agent_id == "test-agent"
    assert agent.languages.primary == "hi-IN"
    assert agent.greeting.opening.startswith("Namaste")
    assert agent.models.stt.provider == "sarvam" and agent.models.llm.model == "gpt-4o-mini"
    tts = agent.models.tts
    if preset == "sarvam":
        assert (tts.provider, tts.model, tts.voice) == ("sarvam", "bulbul:v3", "priya")
    else:
        assert (tts.provider, tts.model, tts.voice) == ("elevenlabs", "eleven_flash_v2_5", "21m00Tcm4TlvDq8ikWAM")

    # And the engine can build the real services from it.
    ctx = CallContext(agent=agent, settings=get_settings(), sample_rate=8000)
    for kind, choice in (("stt", agent.models.stt), ("llm", agent.models.llm), ("tts", tts)):
        providers.build(kind, choice, ctx)


def test_delete_removes_agent_and_mapping(env):
    r, env_args = env
    assert seed.main(["--exophone", "+918047112233", *env_args]) == 0
    assert seed.main(["--exophone", "+918047112233", "--delete", *env_args]) == 0
    assert r.keys("*") == []
    with pytest.raises(store.AgentNotFound):
        _resolve("08047112233")


def test_refuses_to_take_over_a_number_mapped_elsewhere(env):
    r, env_args = env
    r.set(f"{PREFIX}sakhii:voice:number:+918047112233", "real-agent-7")
    assert seed.main(["--exophone", "08047112233", *env_args]) == 1
    assert r.get(f"{PREFIX}sakhii:voice:number:+918047112233") == "real-agent-7"
    assert seed.main(["--exophone", "08047112233", "--delete", *env_args]) == 0
    assert r.get(f"{PREFIX}sakhii:voice:number:+918047112233") == "real-agent-7"


def test_elevenlabs_needs_a_voice_id(env):
    _, env_args = env
    with pytest.raises(SystemExit):
        seed.main(["--exophone", "08047112233", "--provider-preset", "elevenlabs", *env_args])
