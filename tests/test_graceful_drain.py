"""SIGTERM on a running instance (what a deploy does): the port closes at
once, live calls carry on, and the process exits when they end. Runs the real
`app.serve` as a subprocess; needs a local redis-server."""

import asyncio
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import redis
import websockets

REPO = Path(__file__).parent.parent
PREFIX = "sakhii-test-"
REDIS_URL = "redis://127.0.0.1:6379/15"
SILENCE = base64.b64encode(bytes(320)).decode()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
def instance(agent_dict):
    r = redis.Redis.from_url(REDIS_URL)
    try:
        r.ping()
    except redis.ConnectionError:
        pytest.skip("no local redis-server")
    r.flushdb()
    agent_dict["models"] = {k: {"provider": "stub"} for k in ("stt", "llm", "tts")}
    r.set(PREFIX + "sakhii:voice:agent:42", json.dumps(agent_dict))
    r.set(PREFIX + "sakhii:voice:number:+918047112233", "42")

    port = _free_port()
    env = {**os.environ, "REDIS_URL": REDIS_URL, "REDIS_KEY_PREFIX": PREFIX, "OPENAI_API_KEY": "test",
           "EXOTEL_WS_TOKEN": "", "DRAIN_TIMEOUT_SECS": "30"}
    proc = subprocess.Popen([sys.executable, "-m", "tests.serve_with_stubs", "--port", str(port)],
                            cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 20
    while not _port_open(port):
        assert proc.poll() is None, proc.stderr.read()
        assert time.monotonic() < deadline, "instance never started"
        time.sleep(0.1)
    yield proc, port
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    r.flushdb()


async def _call(port: int, sid: str):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/exotel")
    await ws.send(json.dumps({"event": "connected"}))
    await ws.send(json.dumps({"event": "start", "start": {
        "stream_sid": sid, "call_sid": sid, "account_sid": "a", "from": "09999999999", "to": "08047112233"}}))

    async def pump():
        while True:
            await ws.send(json.dumps({"event": "media", "media": {"payload": SILENCE}}))
            await asyncio.sleep(0.02)

    task = asyncio.create_task(pump())
    while json.loads(await asyncio.wait_for(ws.recv(), 5)).get("event") != "media":
        pass
    return ws, task


async def test_sigterm_drains_live_calls(instance):
    proc, port = instance
    ws, pump = await _call(port, "CA-live")

    proc.send_signal(signal.SIGTERM)
    t0 = time.monotonic()
    while _port_open(port):
        assert time.monotonic() - t0 < 2, "port still accepting after SIGTERM"
        await asyncio.sleep(0.05)

    # The live call outlives the shutdown request: still streaming, still answered.
    await asyncio.sleep(3)
    assert proc.poll() is None, "instance exited while a call was live"
    await asyncio.wait_for(ws.ping(), 2)
    assert ws.close_code is None

    # When the call ends, the instance finishes shutting down.
    pump.cancel()
    await ws.close()
    proc.wait(timeout=10)


async def test_idle_instance_stops_promptly(instance):
    proc, port = instance
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)
