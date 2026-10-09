"""After-call analysis → call.analyzed."""

import json
from types import SimpleNamespace

import pytest

from app import analysis, store
from app.agent_config import AgentConfig
from app.settings import get_settings

TRANSCRIPT = [
    {"role": "assistant", "text": "Namaste, main Sakhii bol rahi hoon."},
    {"role": "user", "text": "Mujhe EMI 15 tarikh tak bharna hai, 5000 rupaye."},
]


class FakeClient:
    def __init__(self, reply: dict | Exception):
        self.reply, self.requests = reply, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.reply)))])


@pytest.fixture
def agent(agent_dict, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    get_settings.cache_clear()
    agent_dict["analysis"] = {
        "outcomes": ["promise_to_pay", "dispute", "no_conversation"],
        "fields": [{"key": "amount", "description": "Amount the caller agreed to pay"}, {"key": "pay_by"}],
    }
    yield AgentConfig.model_validate(agent_dict)
    get_settings.cache_clear()


async def test_summary_outcome_sentiment_fields(agent):
    client = FakeClient({
        "summary": "Caller agreed to pay the EMI.\nRs 5000 by the 15th.",
        "summary_local": "Caller ne EMI bharne ka vaada kiya.",
        "outcome": "promise_to_pay", "sentiment": "positive",
        "fields": {"amount": 5000, "pay_by": "15th", "extra": "dropped"},
    })
    result = await analysis.analyze(agent, TRANSCRIPT, "hi-IN", client=client)

    assert result == {
        "summary": "Caller agreed to pay the EMI.\nRs 5000 by the 15th.",
        "summary_local": "Caller ne EMI bharne ka vaada kiya.",
        "language": "hi-IN", "outcome": "promise_to_pay", "sentiment": "positive",
        "fields": {"amount": 5000, "pay_by": "15th"},
    }
    prompt = client.requests[0]["messages"][0]["content"]
    assert '"promise_to_pay", "dispute", "no_conversation"' in prompt and "Hindi" in prompt
    assert client.requests[0]["response_format"] == {"type": "json_object"}


async def test_values_outside_the_lists_become_null(agent):
    client = FakeClient({"summary": "x", "summary_local": "y", "outcome": "sold_a_car", "sentiment": "ecstatic"})
    result = await analysis.analyze(agent, TRANSCRIPT, "en-IN", client=client)
    assert result["outcome"] is None and result["sentiment"] is None
    assert result["summary_local"] is None  # English call: no second summary
    assert result["fields"] == {"amount": None, "pay_by": None}


async def test_skips_calls_where_the_caller_never_spoke(agent):
    client = FakeClient({})
    assert await analysis.analyze(agent, TRANSCRIPT[:1], "hi-IN", client=client) is None
    assert not client.requests


async def test_off_per_agent(agent_dict):
    agent_dict["analysis"] = {"enabled": False}
    assert await analysis.analyze(AgentConfig.model_validate(agent_dict), TRANSCRIPT, "hi-IN", client=FakeClient({})) is None


async def test_emits_call_analyzed(agent, monkeypatch):
    emitted = []

    async def fake_analyze(*_args, **_kw):
        return {"summary": "s", "outcome": "dispute"}

    async def fake_emit(call_sid, data):
        emitted.append((call_sid, data))

    monkeypatch.setattr(analysis, "analyze", fake_analyze)
    monkeypatch.setattr(store, "call_analyzed", fake_emit)
    await analysis.analyze_and_emit("CA9", agent, TRANSCRIPT, "hi-IN")
    assert emitted == [("CA9", {"agent_id": "42", "summary": "s", "outcome": "dispute"})]


async def test_failures_are_swallowed(agent, monkeypatch):
    async def boom(*_args, **_kw):
        raise TimeoutError

    monkeypatch.setattr(analysis, "analyze", boom)
    await analysis.analyze_and_emit("CA10", agent, TRANSCRIPT, "hi-IN")  # no exception
