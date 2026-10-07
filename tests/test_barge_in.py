"""Barge-in and Exotel media framing, over a real WebSocket and real Redis.

Real Silero VAD runs on real (synthesised) speech from tests/fixtures; the
LLM is the real OpenAI service streaming from a local fake server. Needs a
local redis-server.
"""

import array
import asyncio
import base64
import json
import re
import socket
import time
import wave
from pathlib import Path

import pytest
import redis.asyncio as redis
import uvicorn
import websockets
from loguru import logger
from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams

from app import providers, store
from app.exotel import (
    CHUNK_MULTIPLE_BYTES,
    MAX_CHUNK_BYTES,
    MIN_CHUNK_BYTES,
    conform,
    media_chunk_10ms_units,
)
from app.settings import get_settings
from app.turns import SMART_TURN_SILENCE_FALLBACK, TracedSmartTurnAnalyzer
from tests import barge_in_stubs as stubs

PREFIX = "sakhii-test-"
REDIS_URL = "redis://127.0.0.1:6379/15"
FIXTURES = Path(__file__).parent / "fixtures"
FRAME = 320  # 20 ms at 8 kHz, what Exotel sends us


def _pcm(name: str) -> bytes:
    with wave.open(str(FIXTURES / name)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (8000, 1, 2)
        return w.readframes(w.getnframes())


QUESTION = _pcm("caller_question.wav")  # ~2.8 s of speech
BURST = _pcm("short_burst.wav")  # 250 ms of speech, a cough-length sound

LONG_GREETING = " ".join(
    ["Namaste, main Sakhii bol rahi hoon. Aapke loan account ke baare mein ek zaroori baat hai."] * 2
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _serve(app, port):
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.05)
    return srv, task


@pytest.fixture
async def engine(monkeypatch, agent_dict):
    """Returns a function that starts a call for an agent with the given greeting."""
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("REDIS_KEY_PREFIX", PREFIX)
    monkeypatch.setenv("EXOTEL_WS_TOKEN", "secret")
    # Fixed silence threshold instead of the Smart Turn model, so turn ends
    # don't depend on what the model makes of synthetic speech.
    monkeypatch.setenv("TURN_DETECTION", "off")
    get_settings.cache_clear()
    await store.close()
    monkeypatch.setitem(providers.STT, "stub", "tests.barge_in_stubs:build_stt")
    monkeypatch.setitem(providers.LLM, "stub", "tests.barge_in_stubs:build_llm")
    monkeypatch.setitem(providers.TTS, "stub", "tests.barge_in_stubs:build_tts")
    stubs.State.reset()

    r = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await r.ping()
    except redis.ConnectionError:
        pytest.skip("no local redis-server")
    await r.flushdb()

    stubs.State.llm_port = _free_port()
    llm_srv, llm_task = await _serve(stubs.llm_app, stubs.State.llm_port)
    from app.main import app

    port = _free_port()
    srv, task = await _serve(app, port)
    clients = []

    async def call(greeting: str) -> "FakeExotel":
        agent = dict(agent_dict)
        agent["models"] = {k: {"provider": "stub"} for k in ("stt", "llm", "tts")}
        agent["greeting"] = {"opening": greeting, "closing": ""}
        agent["tools"] = []
        await r.set(PREFIX + "sakhii:voice:agent:42", json.dumps(agent))
        await r.set(PREFIX + "sakhii:voice:number:+918047112233", "42")
        client = FakeExotel(await websockets.connect(f"ws://127.0.0.1:{port}/ws/exotel?token=secret"))
        await client.start()
        clients.append(client)
        return client

    yield call

    for c in clients:
        await c.close()
    for s, t in ((srv, task), (llm_srv, llm_task)):
        s.should_exit = True
        await t
    await r.flushdb()
    await r.aclose()
    get_settings.cache_clear()


class FakeExotel:
    """Streams caller audio in real time (silence unless told to speak) and
    records everything the engine sends, with arrival times."""

    def __init__(self, ws):
        self.ws = ws
        self.received: list[tuple[float, dict]] = []
        self._speech = bytearray()
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        await self.ws.send(json.dumps({"event": "connected"}))
        await self.ws.send(json.dumps({
            "event": "start", "stream_sid": "ST1",
            "start": {"stream_sid": "ST1", "call_sid": "CA1", "account_sid": "acme",
                      "from": "09876543210", "to": "08047112233", "custom_parameters": {}},
        }))
        self._tasks = [asyncio.create_task(self._send()), asyncio.create_task(self._read())]

    async def _send(self):
        next_at = time.monotonic()
        while True:
            chunk = bytes(self._speech[:FRAME]).ljust(FRAME, b"\x00")
            del self._speech[:FRAME]
            payload = base64.b64encode(chunk).decode()
            await self.ws.send(json.dumps({"event": "media", "stream_sid": "ST1", "media": {"payload": payload}}))
            next_at += 0.02
            await asyncio.sleep(max(0, next_at - time.monotonic()))

    async def _read(self):
        async for raw in self.ws:
            self.received.append((time.monotonic(), json.loads(raw)))

    async def say(self, pcm: bytes):
        """Speak, returning once the audio has been sent."""
        self._speech.extend(pcm)
        while self._speech:
            await asyncio.sleep(0.02)

    async def wait_for(self, predicate, timeout: float, what: str, after: float = 0.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for i, (t, msg) in enumerate(self.received):
                if t > after and predicate(msg):
                    return i, t
            await asyncio.sleep(0.02)
        raise AssertionError(f"timed out waiting for {what}")

    def media(self) -> list[tuple[int, float, bytes]]:
        return [
            (i, t, base64.b64decode(m["media"]["payload"]))
            for i, (t, m) in enumerate(self.received)
            if m.get("event") == "media"
        ]

    async def close(self):
        for t in self._tasks:
            t.cancel()
        await self.ws.close()


def amplitude(pcm: bytes) -> int:
    samples = array.array("h", pcm)
    return max((abs(s) for s in samples), default=0)


def is_audio_of(marker: str):
    level = stubs.AMPLITUDE[marker]

    def check(msg):
        return msg.get("event") == "media" and amplitude(base64.b64decode(msg["media"]["payload"])) == level

    return check


def is_clear(msg):
    return msg.get("event") == "clear"


def assert_exotel_media_rules(client: FakeExotel):
    """Every media message: 8 kHz 16-bit PCM in base64, >= 3,200 bytes,
    <= 100,000 bytes, and a multiple of 320 bytes."""
    media = client.media()
    assert media, "engine sent no audio"
    for i, _, pcm in media:
        msg = client.received[i][1]
        assert msg["stream_sid"] == "ST1"
        assert len(pcm) % CHUNK_MULTIPLE_BYTES == 0, f"message {i}: {len(pcm)} bytes"
        assert MIN_CHUNK_BYTES <= len(pcm) <= MAX_CHUNK_BYTES, f"message {i}: {len(pcm)} bytes"


# --- tests -----------------------------------------------------------------


def test_chunk_size_math():
    assert media_chunk_10ms_units(8000) * 160 == 3200
    for rate in (8000, 16000, 24000):
        size = media_chunk_10ms_units(rate) * rate // 100 * 2
        assert size % CHUNK_MULTIPLE_BYTES == 0 and MIN_CHUNK_BYTES <= size <= MAX_CHUNK_BYTES
    assert len(conform(b"\x01\x00" * 10)) == MIN_CHUNK_BYTES
    assert len(conform(bytes(3201))) == 3520


async def test_reply_audio_meets_exotel_chunk_rules(engine):
    client = await engine("")
    await client.say(bytes(8000))
    await client.say(QUESTION)
    await client.wait_for(is_audio_of("alpha"), 10, "the reply")
    # Let the whole reply play out, including its final, padded chunk.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        last = max((t for _, t, pcm in client.media() if amplitude(pcm)), default=0)
        if last and time.monotonic() - last > 1.5:
            break
        await asyncio.sleep(0.1)

    assert_exotel_media_rules(client)
    reply_started = next(t for t, m in client.received if is_audio_of("alpha")(m))
    reply = [pcm for _, _, pcm in client.media() if amplitude(pcm) == stubs.AMPLITUDE["alpha"]]
    # The reply's last chunk was a partial one, padded with silence by us.
    assert reply[-1].endswith(b"\x00\x00" * 10)
    assert len(reply[-1]) == MIN_CHUNK_BYTES
    # (A clear goes out when every caller turn starts; none may cut the reply.)
    assert not any(is_clear(m) for t, m in client.received if t > reply_started)


async def test_caller_interrupts_mid_reply(engine):
    client = await engine("")
    await client.say(bytes(8000))
    await client.say(QUESTION)
    _, first_audio = await client.wait_for(is_audio_of("alpha"), 10, "the first reply")
    await asyncio.sleep(0.8)  # the caller listens for a bit...

    spoke_at = time.monotonic()
    speaking = asyncio.create_task(client.say(QUESTION))  # ...then talks over the bot
    clear_idx, clear_at = await client.wait_for(is_clear, 5, "a clear event", after=spoke_at)
    await speaking
    await client.wait_for(is_audio_of("bravo"), 15, "the reply to the interruption")

    # (c) Exotel's documented clear format.
    assert client.received[clear_idx][1] == {"event": "clear", "stream_sid": "ST1"}
    # Interruption came after the guard window, not on the first syllable.
    reaction = clear_at - spoke_at
    print(f"barge-in reaction {reaction:.2f}s")
    assert 0.3 < reaction < 1.2, f"barge-in took {reaction:.2f}s"
    # (b) Nothing of the interrupted reply is sent after the clear.
    old = stubs.AMPLITUDE["alpha"]
    late = [i for i, _, pcm in client.media() if i > clear_idx and amplitude(pcm) == old]
    assert not late, f"{len(late)} chunks of the old reply sent after clear"
    # (a) The LLM stream was cut off, and TTS started nothing more for it.
    assert stubs.State.llm_cancelled == [1]
    assert all(t < clear_at for t, m in stubs.State.tts_started if m == "alpha")
    # New turn: the second request carries the cut-off reply and the caller's speech.
    assert len(stubs.State.llm_requests) == 2
    second = stubs.State.llm_requests[1]
    assert second[-1]["role"] == "user" and "word" in second[-1]["content"]
    assert any(m["role"] == "assistant" and "alpha" in str(m["content"]) for m in second)
    assert_exotel_media_rules(client)


async def test_short_noise_does_not_interrupt(engine):
    client = await engine(LONG_GREETING)
    await client.wait_for(is_audio_of("greeting"), 10, "the greeting")
    await asyncio.sleep(0.5)
    burst_start = time.monotonic()
    await client.say(BURST)
    burst_end = time.monotonic()
    await asyncio.sleep(2.0)

    clears = [m for t, m in client.received if t > burst_start and is_clear(m)]
    assert not clears, "a 250 ms sound interrupted the bot"
    assert stubs.State.llm_requests == []
    greeting_after = [t for _, t, pcm in client.media() if amplitude(pcm) == 1000 and t > burst_end + 1.0]
    assert greeting_after, "greeting audio stopped after the noise"
    assert_exotel_media_rules(client)


async def test_caller_interrupts_greeting(engine):
    client = await engine(LONG_GREETING)
    await client.wait_for(is_audio_of("greeting"), 10, "the greeting")
    await asyncio.sleep(0.5)
    spoke_at = time.monotonic()
    speaking = asyncio.create_task(client.say(QUESTION))
    clear_idx, _ = await client.wait_for(is_clear, 5, "a clear event", after=spoke_at)
    await speaking
    await client.wait_for(is_audio_of("alpha"), 15, "the reply")

    assert client.received[clear_idx][1] == {"event": "clear", "stream_sid": "ST1"}
    late = [i for i, _, pcm in client.media() if i > clear_idx and amplitude(pcm) == 1000]
    assert not late, f"{len(late)} greeting chunks sent after clear"
    assert len(stubs.State.llm_requests) == 1
    first = stubs.State.llm_requests[0]
    assert first[-1]["role"] == "user" and "word" in first[-1]["content"]
    assert_exotel_media_rules(client)


async def test_smart_turn_logs_why_each_turn_ended(engine, monkeypatch):
    monkeypatch.setenv("TURN_DETECTION", "smart")
    get_settings.cache_clear()
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), filter=lambda r: "turn ended" in r["message"])
    try:
        client = await engine("")
        await client.say(bytes(8000))
        await client.say(QUESTION)
        # Smart Turn may treat the pause after "Wait," as the end of a turn,
        # in which case the second reply is the one that plays.
        await client.wait_for(
            lambda m: is_audio_of("alpha")(m) or is_audio_of("bravo")(m), 15, "a reply"
        )
    finally:
        logger.remove(sink)
    assert lines, "no per-turn log line"
    assert re.match(r"turn ended: reason=(smart_turn|smart_turn_silence_fallback) p_complete=", lines[-1])


async def test_smart_turn_silence_fallback_is_labelled():
    analyzer = TracedSmartTurnAnalyzer(params=SmartTurnParams(stop_secs=0.3))
    analyzer.set_sample_rate(8000)
    speech = QUESTION[:16000]
    for i in range(0, len(speech), FRAME):
        analyzer.append_audio(speech[i:i + FRAME], True)
    state = None
    for _ in range(50):
        state = analyzer.append_audio(bytes(FRAME), False)
        if state == EndOfTurnState.COMPLETE:
            break
    assert state == EndOfTurnState.COMPLETE
    await analyzer.analyze_end_of_turn()
    assert analyzer.last_reason == SMART_TURN_SILENCE_FALLBACK
