"""Real-time notifications for the Sakhii dashboard: wss://.../ws/dashboard.

The browser gets a short-lived token from Laravel (GET /api/user/realtime),
opens the socket and sends {"type": "auth", "token": "..."} as its first
message (not in the URL, so it never reaches an access log). From then on it
receives every notification for its account.

Notifications travel over Redis pub/sub, channel
sakhii:voice:notify:<client_id> (with REDIS_KEY_PREFIX):
  - Laravel publishes call logs, summaries and recordings as they're saved;
  - this engine publishes live calls starting and ending.
One relay task forwards them to the sockets of that account, so it works the
same whichever process published.

Token: base64url(JSON {"c": client_id, "exp": unix}) + "." +
base64url(HMAC-SHA256(k, first part)), k = HMAC-SHA256(SAKHII_VOICE_CRED_KEY
bytes, "sakhii:voice:dashboard"). Laravel signs, the engine verifies.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from app import credentials, store

TOKEN_CONTEXT = b"sakhii:voice:dashboard"
AUTH_TIMEOUT_SECS = 5
MAX_SOCKETS_PER_ACCOUNT = 20

_sockets: dict[str, set[WebSocket]] = defaultdict(set)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signing_key() -> bytes:
    return hmac.new(credentials._key(), TOKEN_CONTEXT, hashlib.sha256).digest()


def sign(client_id: str | int, ttl_secs: int = 600) -> str:
    """What Laravel does (here for tests and tools)."""
    payload = _b64(json.dumps({"c": str(client_id), "exp": int(time.time()) + ttl_secs}).encode())
    return payload + "." + _b64(hmac.new(_signing_key(), payload.encode(), hashlib.sha256).digest())


def verify(token: str) -> str | None:
    """The account id the token was issued for, or None."""
    try:
        payload, signature = token.split(".", 1)
        expected = hmac.new(_signing_key(), payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(signature)):
            return None
        claims = json.loads(_unb64(payload))
        if int(claims["exp"]) < time.time():
            return None
        return str(claims["c"]) or None
    except Exception:
        return None


def connected(client_id: str | None = None) -> int:
    return len(_sockets.get(client_id, ())) if client_id else sum(len(s) for s in _sockets.values())


async def serve(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        first = await asyncio.wait_for(websocket.receive_json(), AUTH_TIMEOUT_SECS)
        client_id = verify(str(first.get("token", ""))) if isinstance(first, dict) and first.get("type") == "auth" else None
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, KeyError):
        client_id = None
    if client_id is None:
        await _close(websocket, 1008)
        return
    if len(_sockets[client_id]) >= MAX_SOCKETS_PER_ACCOUNT:
        await _close(websocket, 1013)
        return

    _sockets[client_id].add(websocket)
    try:
        await websocket.send_json({"type": "ready"})
        while True:  # nothing to receive; this just notices the disconnect
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        _sockets[client_id].discard(websocket)
        if not _sockets[client_id]:
            _sockets.pop(client_id, None)


async def _close(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.close(code=code)
    except RuntimeError:
        pass


async def notify(client_id: str | int | None, notification: dict[str, Any]) -> None:
    """Publish to an account's dashboards. Never raises: notifications are best-effort."""
    if not client_id:
        return
    try:
        await store.client().publish(store.key("notify", str(client_id)), json.dumps(notification))
    except Exception as e:
        logger.warning("Dashboard notification failed: {}", type(e).__name__)


async def _deliver(client_id: str, message: str) -> None:
    for websocket in list(_sockets.get(client_id, ())):
        try:
            await websocket.send_text(message)
        except Exception:
            _sockets[client_id].discard(websocket)


async def relay() -> None:
    """Redis pub/sub → the sockets of each account. Reconnects if Redis drops."""
    pattern = store.key("notify", "*")
    prefix_len = len(pattern) - 1
    while True:
        pubsub = store.client().pubsub()
        try:
            await pubsub.psubscribe(pattern)
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30)
                if message and message.get("type") == "pmessage":
                    await _deliver(str(message["channel"])[prefix_len:], str(message["data"]))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Dashboard relay lost Redis ({}); retrying", type(e).__name__)
            await asyncio.sleep(2)
        finally:
            try:
                await pubsub.aclose()
            except Exception:
                pass
