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


async def _greeted(url: str, call_sid: str) -> bool:
    """Run a short call; True if the engine answered with audio."""
    silence = base64.b64encode(b"\x00\x00" * 160).decode()
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(_start(call_sid=call_sid))
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            await ws.send(json.dumps({"event": "media", "stream_sid": "ST1", "media": {"payload": silence}}))
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 0.05))
            except TimeoutError:
                continue
            if msg.get("event") == "media":
                return True
    return False


async def test_token_as_path_segment(server):
    """The form Exotel's Voicebot applet can use (it drops query strings)."""
    port, r = server
    assert await _greeted(f"ws://127.0.0.1:{port}/ws/exotel/secret", "CA123")
    assert stub_providers.SPOKEN and "Ramesh ji" in stub_providers.SPOKEN[0]


async def test_token_as_query_parameter_still_works(server):
    port, _ = server
    assert await _greeted(f"ws://127.0.0.1:{port}/ws/exotel?token=secret", "CA123")


@pytest.mark.parametrize(
    "path",
    [
        "/ws/exotel",  # what Exotel sent before: no token at all
        "/ws/exotel/nope",
        "/ws/exotel/secre",
        "/ws/exotel/secret-and-more",
        "/ws/exotel/s%C3%A9cret",  # non-ASCII: a mismatch, not a server error
        "/ws/exotel?token=",
    ],
)
async def test_rejects_missing_or_bad_tokens(server, path):
    port, _ = server
    with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
        async with websockets.connect(f"ws://127.0.0.1:{port}{path}") as ws:
            await ws.recv()
    assert exc.value.response.status_code == 403


async def test_token_never_reaches_the_logs(server):
    """uvicorn logs every WebSocket handshake with its full path; ours and
    theirs must show the token masked, for good and bad tokens alike."""
    import logging

    from loguru import logger

    port, _ = server
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    uv = logging.getLogger("uvicorn.error")
    handler, old_level = Capture(), uv.level
    uv.addHandler(handler)
    uv.setLevel(logging.INFO)
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="DEBUG")
    try:
        assert await _greeted(f"ws://127.0.0.1:{port}/ws/exotel/secret", "CA123")
        assert await _greeted(f"ws://127.0.0.1:{port}/ws/exotel?token=secret&agent_id=42", "CA123")
        for bad in ("/ws/exotel/wrongtoken123", "/ws/exotel?token=wrongtoken456"):
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(f"ws://127.0.0.1:{port}{bad}") as ws:
                    await ws.recv()
        await asyncio.sleep(0.3)
    finally:
        uv.removeHandler(handler)
        uv.setLevel(old_level)
        logger.remove(sink)

    handshakes = [line for line in lines if '"WebSocket ' in line]
    assert len(handshakes) >= 4, lines
    for secret in ("secret", "wrongtoken123", "wrongtoken456"):
        assert not [line for line in lines if secret in line], f"{secret!r} leaked"
    assert any("/ws/exotel/***" in line for line in handshakes)
    assert any("token=***&agent_id=42" in line for line in handshakes)


async def _call_and_wait(port, r, seconds=1.2):
    silence = base64.b64encode(b"\x00\x00" * 160).decode()
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/exotel?token=secret") as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(_start())
        for i in range(int(seconds / 0.02)):
            await ws.send(json.dumps({"event": "media", "stream_sid": "ST1", "media": {"chunk": i, "payload": silence}}))
            await asyncio.sleep(0.02)
        await ws.send(json.dumps({"event": "stop", "stream_sid": "ST1", "stop": {"call_sid": "CA123"}}))
    for _ in range(100):
        state = await r.hgetall(PREFIX + "sakhii:voice:call:CA123")
        if state.get("status") == "completed":
            return state
        await asyncio.sleep(0.1)
    raise AssertionError("call never completed")


async def test_recording_lands_in_call_ended(server, monkeypatch):
    from app import recording

    port, r = server
    for k, v in {"RECORDING_S3_BUCKET": "calls", "RECORDING_S3_KEY": "k", "RECORDING_S3_SECRET": "s",
                 "RECORDING_PUBLIC_BASE_URL": "https://rec.example.com"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    uploaded = []

    def fake_upload(path, key, _s):
        with open(path, "rb") as f:
            uploaded.append((key, f.read()))

    monkeypatch.setattr(recording, "upload", fake_upload)
    raw = json.loads(await r.get(PREFIX + "sakhii:voice:agent:42"))
    raw["recording_enabled"] = True
    await r.set(PREFIX + "sakhii:voice:agent:42", json.dumps(raw))

    state = await _call_and_wait(port, r)

    assert len(uploaded) == 1
    key, mp3 = uploaded[0]
    assert key.endswith("/CA123.mp3") and len(mp3) > 0
    assert state["recording_key"] == key
    assert state["recording_url"] == f"https://rec.example.com/{key}"
    assert float(state["recording_duration"]) > 0.5
    ended = [json.loads(e[1]["data"]) for e in await r.xrange(PREFIX + "sakhii:voice:events") if e[1]["type"] == "call.ended"]
    assert ended[0]["recording_key"] == key and ended[0]["recording_duration"] > 0.5


async def test_no_recording_fields_when_off(server):
    port, r = server
    state = await _call_and_wait(port, r, seconds=0.4)
    assert "recording_key" not in state
    ended = [json.loads(e[1]["data"]) for e in await r.xrange(PREFIX + "sakhii:voice:events") if e[1]["type"] == "call.ended"]
    assert ended[0]["recording_key"] is None and ended[0]["recording_url"] is None


async def test_missing_credential_closes_the_call(server):
    port, r = server
    raw = json.loads(await r.get(PREFIX + "sakhii:voice:agent:42"))
    raw["models"]["tts"]["credential_id"] = "999"
    await r.set(PREFIX + "sakhii:voice:agent:42", json.dumps(raw))
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/exotel?token=secret") as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(_start())
        with pytest.raises(websockets.exceptions.ConnectionClosed) as exc:
            await asyncio.wait_for(ws.recv(), 5)
    assert exc.value.rcvd.code == 1011


async def test_live_call_keeps_its_settings_through_a_hot_reload(server, monkeypatch):
    """Settings change mid-call (pub/sub, picked up by the running engine): the
    live call finishes on the old ones, the next call uses the new ones."""
    from app import live_settings, recording
    from tests.test_live_settings import KEY, seal

    port, r = server
    monkeypatch.setenv("SAKHII_VOICE_CRED_KEY", KEY)
    get_settings.cache_clear()  # bootstrap value: as if the engine had started with it
    uploaded = []
    monkeypatch.setattr(recording, "upload", lambda path, key, s: uploaded.append(key))
    raw = json.loads(await r.get(PREFIX + "sakhii:voice:agent:42"))
    raw["recording_enabled"] = True
    await r.set(PREFIX + "sakhii:voice:agent:42", json.dumps(raw))

    async def publish(version, base):
        await r.set(PREFIX + "sakhii:voice:settings", json.dumps({"version": version, "values": {
            "RECORDING_S3_BUCKET": "calls", "RECORDING_PUBLIC_BASE_URL": base}}))
        await r.set(PREFIX + "sakhii:voice:secrets", seal({"version": 1, "fields": {
            "RECORDING_S3_KEY": "AKIA", "RECORDING_S3_SECRET": "s3-secret"}}))
        await r.publish(PREFIX + "sakhii:voice:settings:changed", str(version))
        for _ in range(100):
            if live_settings.state.settings_version == str(version):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"engine never applied settings v{version}: {live_settings.state}")

    await publish(1, "https://old.example.com")
    silence = base64.b64encode(b"\x00\x00" * 160).decode()
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/exotel/secret") as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(_start())
        for i in range(25):
            await ws.send(json.dumps({"event": "media", "stream_sid": "ST1", "media": {"chunk": i, "payload": silence}}))
            await asyncio.sleep(0.02)
        await publish(2, "https://new.example.com")  # mid-call
        for i in range(25, 50):
            await ws.send(json.dumps({"event": "media", "stream_sid": "ST1", "media": {"chunk": i, "payload": silence}}))
            await asyncio.sleep(0.02)
        await ws.send(json.dumps({"event": "stop", "stream_sid": "ST1", "stop": {"call_sid": "CA123"}}))
    for _ in range(100):
        state = await r.hgetall(PREFIX + "sakhii:voice:call:CA123")
        if state.get("status") == "completed":
            break
        await asyncio.sleep(0.1)
    assert state["recording_url"].startswith("https://old.example.com/")

    await r.delete(PREFIX + "sakhii:voice:call:CA123")
    state = await _call_and_wait(port, r, seconds=0.4)
    assert state["recording_url"].startswith("https://new.example.com/")

    status = json.loads(await r.get(PREFIX + "sakhii:voice:status"))  # published at startup
    assert status["version"] and "active_calls" in status
