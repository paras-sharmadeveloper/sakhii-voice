"""FastAPI entrypoint: wss://voice.YOURDOMAIN.com/ws/exotel"""

import hmac
import sys
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from loguru import logger
from pipecat.runner.utils import parse_telephony_websocket

from app import providers, store, tools
from app.pipeline import run_call, warmup
from app.redact import redact_uvicorn_logs
from app.settings import get_settings

logger.remove()
logger.add(sys.stderr, level=get_settings().log_level, enqueue=False)
redact_uvicorn_logs()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    providers.preload()
    warmup()
    await store.client().ping()
    logger.info("sakhii-voice ready")
    yield
    await tools.close_http()
    await store.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# Live calls in this process; app.serve waits for it to reach 0 on shutdown.
active_calls = 0


@app.get("/healthz")
async def healthz():
    try:
        await store.client().ping()
    except Exception as e:
        return JSONResponse({"ok": False, "redis": str(e)}, status_code=503)
    return {"ok": True}


def _token_ok(given: str) -> bool:
    """Constant-time check. Bytes, so a non-ASCII token is a plain mismatch
    rather than a TypeError from compare_digest."""
    expected = get_settings().exotel_ws_token
    if not expected:
        return True
    return hmac.compare_digest(given.encode(), expected.encode())


@app.websocket("/ws/exotel")
async def exotel(websocket: WebSocket):
    """wss://.../ws/exotel?token=<EXOTEL_WS_TOKEN> (curl, tools)."""
    await _exotel(websocket, websocket.query_params.get("token", ""))


@app.websocket("/ws/exotel/{token}")
async def exotel_path_token(websocket: WebSocket, token: str):
    """wss://.../ws/exotel/<EXOTEL_WS_TOKEN>: Exotel's Voicebot applet drops
    query parameters, so it has to carry the token in the path."""
    await _exotel(websocket, token)


async def _exotel(websocket: WebSocket, token: str):
    s = get_settings()
    if not _token_ok(token):
        # Never log the token itself.
        logger.warning("Rejected Exotel stream: bad token from {}", websocket.client)
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        transport_type, call = await parse_telephony_websocket(websocket)
    except ValueError as e:
        logger.warning("Exotel stream closed before handshake: {}", e)
        return
    if transport_type != "exotel":
        logger.warning("Not an Exotel handshake ({}), closing", transport_type)
        await websocket.close(code=1003)
        return
    if s.exotel_account_sid and call.account_sid != s.exotel_account_sid:
        logger.warning("Rejected Exotel stream for account {}", call.account_sid)
        await websocket.close(code=1008)
        return

    # Agent can be pinned in the Voicebot URL (?agent_id=) or via Exotel
    # custom parameters; otherwise it's looked up by the dialled number.
    custom = call.custom_parameters or {}
    if isinstance(custom, str):
        custom = {k: v[0] for k, v in parse_qs(custom).items()}
    agent_id = websocket.query_params.get("agent_id") or custom.get("agent_id")

    global active_calls
    active_calls += 1
    try:
        await run_call(websocket, call, agent_id)
    except Exception:
        logger.exception("Call {} crashed", call.call_id)
    finally:
        active_calls -= 1
