"""Full call over a real WebSocket and a real Redis (db 15), with stub
providers registered exactly like real ones. Needs a local redis-server."""

import asyncio
import base64
import json
import socket

import pytest
import redis.asyncio as redis
import uvicorn
import websockets

from app import providers, store
from app.settings import get_settings
from tests import stub_providers

PREFIX = "sakhii-test-"
REDIS_URL = "redis://127.0.0.1:6379/15"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server(monkeypatch, agent_dict):
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("REDIS_KEY_PREFIX", PREFIX)
    monkeypatch.setenv("EXOTEL_WS_TOKEN", "secret")
    monkeypatch.setenv("EXOTEL_ACCOUNT_SID", "acme")
    get_settings.cache_clear()
    await store.close()

    # The "one registry line" for each stub provider.
    monkeypatch.setitem(providers.STT, "stub", "tests.stub_providers:build_stt")
    monkeypatch.setitem(providers.LLM, "stub", "tests.stub_providers:build_llm")
    monkeypatch.setitem(providers.TTS, "stub", "tests.stub_providers:build_tts")

    r = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await r.ping()
    except redis.ConnectionError:
        pytest.skip("no local redis-server")
    await r.flushdb()
    agent_dict["models"] = {k: {"provider": "stub"} for k in ("stt", "llm", "tts")}
    await r.set(PREFIX + "sakhii:voice:agent:42", json.dumps(agent_dict))
    await r.set(PREFIX + "sakhii:voice:number:+918047112233", "42")
    await r.set(
        PREFIX + "sakhii:voice:call:CA123:init",
        json.dumps({"variables": {"customer_name": "Ramesh", "amount_due": "5000"}}),
    )

    from app.main import app

    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.05)
    stub_providers.SPOKEN.clear()
    yield port, r
    srv.should_exit = True
    await task
    await r.flushdb()
    await r.aclose()
    get_settings.cache_clear()


def _start(call_sid="CA123", to="08047112233", account="acme"):
    return json.dumps(
        {
            "event": "start",
            "sequence_number": 1,
            "stream_sid": "ST1",
            "start": {
                "stream_sid": "ST1",
                "call_sid": call_sid,
                "account_sid": account,
                "from": "09876543210",
                "to": to,
                "custom_parameters": {},
            },
        }
    )


async def test_inbound_call_greets_and_logs(server):
    port, r = server
    url = f"ws://127.0.0.1:{port}/ws/exotel?token=secret"
    silence = base64.b64encode(b"\x00\x00" * 160).decode()  # 20 ms @ 8 kHz
    received = []

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(_start())

        async def reader():
            async for msg in ws:
                received.append(json.loads(msg))

        reader_task = asyncio.create_task(reader())
        for i in range(60):
            await ws.send(json.dumps({"event": "media", "stream_sid": "ST1", "media": {"chunk": i, "payload": silence}}))
            await asyncio.sleep(0.02)
        await ws.send(json.dumps({"event": "stop", "stream_sid": "ST1", "stop": {"call_sid": "CA123"}}))
        await ws.close()
        reader_task.cancel()

    media = [m for m in received if m.get("event") == "media"]
    assert media, f"no audio back from engine: {received[:3]}"
    assert all(m["stream_sid"] == "ST1" for m in media)
    assert stub_providers.SPOKEN[0].startswith("Namaste, main Sakhii bol rahi hoon Acme Finance se")
    assert "Ramesh ji" in stub_providers.SPOKEN[0]

    for _ in range(50):
        state = await r.hgetall(PREFIX + "sakhii:voice:call:CA123")
        if state.get("status") == "completed":
            break
        await asyncio.sleep(0.1)
    assert state["status"] == "completed"
    assert state["agent_id"] == "42"
    assert state["end_reason"] == "caller_hung_up"
    transcript = json.loads(state["transcript"])
    assert transcript[0]["role"] == "assistant" and "Ramesh" in transcript[0]["text"]

    events = await r.xrange(PREFIX + "sakhii:voice:events")
    assert [e[1]["type"] for e in events] == ["call.started", "call.ended"]
    assert not await r.smembers(PREFIX + "sakhii:voice:active")


async def test_rejects_bad_token(server):
    port, _ = server
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/exotel?token=nope") as ws:
            await ws.recv()


async def test_unknown_number_closes(server):
    port, _ = server
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/exotel?token=secret") as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(_start(call_sid="CA999", to="08000000000"))
        with pytest.raises(websockets.exceptions.ConnectionClosed) as exc:
            await asyncio.wait_for(ws.recv(), 5)
    assert exc.value.rcvd.code == 1011


async def test_time_limit_speaks_then_hangs_up(server):
    """Same mechanism end_call / transfer_call use: last line, then we close."""
    port, r = server
    raw = json.loads(await r.get(PREFIX + "sakhii:voice:agent:42"))
    raw["max_call_duration_secs"] = 1
    await r.set(PREFIX + "sakhii:voice:agent:42", json.dumps(raw))
    silence = base64.b64encode(b"\x00\x00" * 160).decode()

    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/exotel?token=secret") as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(_start())

        async def feed():
            for i in range(250):
                await ws.send(json.dumps({"event": "media", "stream_sid": "ST1", "media": {"chunk": i, "payload": silence}}))
                await asyncio.sleep(0.02)

        feeder = asyncio.create_task(feed())
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            while True:
                await asyncio.wait_for(ws.recv(), 6)
        feeder.cancel()

    assert stub_providers.SPOKEN[-1].startswith("Hamara samay poora ho gaya hai")
    for _ in range(50):
        state = await r.hgetall(PREFIX + "sakhii:voice:call:CA123")
        if state.get("status") == "completed":
            break
        await asyncio.sleep(0.1)
    assert state["end_reason"] == "max_duration"
