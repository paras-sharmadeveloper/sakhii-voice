"""FastAPI entrypoint: wss://voice.YOURDOMAIN.com/ws/exotel"""

import asyncio
import hmac
import sys
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from loguru import logger
from pipecat.runner.utils import parse_telephony_websocket

from app import dashboard, live_settings, providers, status, store, tools
from app.catalog import build_catalog
from app.pipeline import run_call, warmup
from app.redact import redact_uvicorn_logs
from app.settings import Settings, get_settings, pinned

_log_sink: int | None = None


def configure_logging(s: Settings) -> None:
    """Also a settings hook: LOG_LEVEL changes apply without a restart.
    diagnose=False: tracebacks never print local variables (they can hold keys)."""
    global _log_sink
    if _log_sink is None:
        logger.remove()
    else:
        logger.remove(_log_sink)
    _log_sink = logger.add(sys.stderr, level=s.log_level, enqueue=False, diagnose=False)


configure_logging(get_settings())
redact_uvicorn_logs()
live_settings.on_change(configure_logging)
# The admin panel waits for the reload result; don't make it wait for the 30 s tick.
live_settings.after_reload(lambda: status.publish(active_calls))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await store.client().ping()
    await live_settings.reload(force=True)  # Redis settings before the first call
    providers.preload()
    warmup()
    background = [
        asyncio.create_task(live_settings.watch()),
        asyncio.create_task(status.run(lambda: active_calls)),
        asyncio.create_task(dashboard.relay()),
    ]
    logger.info("sakhii-voice {} ready", status.VERSION)
    yield
    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    await tools.close_http()
    await store.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# Live calls in this process; app.serve waits for it to reach 0 on shutdown.
active_calls = 0


@app.get("/catalog")
async def catalog(request: Request):
    """Internal: providers, models, voices, languages, tuning (Laravel admin)."""
    token = get_settings().engine_admin_token
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if not hmac.compare_digest(given.encode(), token.encode()):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await build_catalog()


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


@app.websocket("/ws/dashboard")
async def dashboard_socket(websocket: WebSocket):
    """Sakhii dashboard notifications (app/dashboard.py). Auth is the first message."""
    await dashboard.serve(websocket)


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
    # The whole call, and every task it starts, keeps this snapshot even if
    # the settings are reloaded meanwhile.
    with pinned(get_settings()):
        await _exotel_call(websocket, token)


async def _exotel_call(websocket: WebSocket, token: str):
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
