"""Dashboard notifications over /ws/dashboard (real WebSocket, real Redis)."""

import asyncio
import base64
import json
import os

import pytest
import websockets

from app import dashboard, store
from app.settings import get_settings
from tests.test_exotel_e2e import PREFIX, _start, server  # noqa: F401  (fixture)

KEY = base64.b64encode(os.urandom(32)).decode()


@pytest.fixture
def cred_key(monkeypatch):
    monkeypatch.setenv("SAKHII_VOICE_CRED_KEY", KEY)
    get_settings.cache_clear()


async def _connect(port, token):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/dashboard")
    await ws.send(json.dumps({"type": "auth", "token": token}))
    return ws


async def _recv(ws, timeout=3.0):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


def test_tokens(cred_key):
    assert dashboard.verify(dashboard.sign(7)) == "7"
    assert dashboard.verify(dashboard.sign(7, ttl_secs=-1)) is None  # expired
    payload, sig = dashboard.sign(7).split(".")
    forged = base64.urlsafe_b64encode(json.dumps({"c": "8", "exp": 9999999999}).encode()).rstrip(b"=").decode()
    assert dashboard.verify(f"{forged}.{sig}") is None
    assert dashboard.verify("garbage") is None


def test_token_signed_with_another_key_is_refused(cred_key, monkeypatch):
    token = dashboard.sign(7)
    monkeypatch.setenv("SAKHII_VOICE_CRED_KEY", base64.b64encode(os.urandom(32)).decode())
    get_settings.cache_clear()
    assert dashboard.verify(token) is None


async def test_bad_or_missing_auth_is_closed(server, cred_key):  # noqa: F811
    port, _ = server
    ws = await _connect(port, "nope")
    with pytest.raises(websockets.exceptions.ConnectionClosed) as e:
        await _recv(ws)
    assert e.value.rcvd.code == 1008

    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/dashboard") as ws:
        with pytest.raises(websockets.exceptions.ConnectionClosed):  # no auth message within 5 s
            await _recv(ws, timeout=7)


async def test_notifications_reach_only_their_account(server, cred_key):  # noqa: F811
    port, r = server
    mine = await _connect(port, dashboard.sign(7))
    other = await _connect(port, dashboard.sign(8))
    assert (await _recv(mine))["type"] == "ready" and (await _recv(other))["type"] == "ready"
    await asyncio.sleep(0.2)  # relay subscribed

    # What Laravel publishes (raw PUBLISH, prefix applied explicitly).
    await r.publish(PREFIX + "sakhii:voice:notify:7", json.dumps({"id": "summary-1", "type": "summary_ready"}))
    assert await _recv(mine) == {"id": "summary-1", "type": "summary_ready"}
    with pytest.raises(asyncio.TimeoutError):
        await _recv(other, timeout=0.5)
    await mine.close()
    await other.close()


async def test_a_live_call_shows_up_and_ends(server, cred_key):  # noqa: F811
    port, r = server
    dash = await _connect(port, dashboard.sign(7))  # tenant_id of the test agent
    assert (await _recv(dash))["type"] == "ready"
    await asyncio.sleep(0.2)

    silence = base64.b64encode(b"\x00\x00" * 160).decode()
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/exotel/secret") as call:
        await call.send(json.dumps({"event": "connected"}))
        await call.send(_start())
        live = await _recv(dash)
        assert live["type"] == "live_call" and live["id"] == "live-CA123"
        assert live["body"] == "Caller: 09876543210"
        for i in range(15):
            await call.send(json.dumps({"event": "media", "stream_sid": "ST1", "media": {"chunk": i, "payload": silence}}))
            await asyncio.sleep(0.02)
        await call.send(json.dumps({"event": "stop", "stream_sid": "ST1", "stop": {"call_sid": "CA123"}}))

    ended = await _recv(dash, timeout=10)
    assert ended["type"] == "live_call_ended" and ended["id"] == "live-CA123"
    await dash.close()


async def test_status_counts_sockets(server, cred_key):  # noqa: F811
    port, _ = server
    dash = await _connect(port, dashboard.sign(7))
    await _recv(dash)
    assert dashboard.connected("7") == 1
    await dash.close()
    for _ in range(50):
        if dashboard.connected("7") == 0:
            break
        await asyncio.sleep(0.05)
    assert dashboard.connected() == 0
    assert store  # imported for the fixture's Redis
