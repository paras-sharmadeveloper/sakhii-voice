"""Engine status for Laravel's admin, in sakhii:voice:status every 30 s.

Expires after 90 s, so a missing key means the engine is down. Provider
health is the last error *type* seen per provider (never messages: they can
carry request details), plus counts since start.
"""

import asyncio
import json
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from app import live_settings, store

INTERVAL_SECS = 30
TTL_SECS = 90
STARTED_AT = time.time()
_providers: dict[str, dict[str, Any]] = {}


def git_version(root: Path = Path(__file__).resolve().parent.parent) -> str:
    """Short commit of the checkout, read from .git directly (no git binary, no
    "dubious ownership" errors when the service user doesn't own the repo)."""
    try:
        git = root / ".git"
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head[:12]
        ref = head[5:]
        ref_file = git / ref
        if ref_file.is_file():
            return ref_file.read_text().strip()[:12]
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split()[0][:12]
    except OSError:
        pass
    return os.environ.get("SAKHII_VOICE_VERSION", "unknown")


VERSION = git_version()


def record(kind: str, provider: str, error: BaseException | str | None = None) -> None:
    """One call's outcome for a provider. `error` is reduced to its type name."""
    entry = _providers.setdefault(f"{kind}.{provider}", {
        "calls": 0, "errors": 0, "last_error": None, "last_error_at": None, "last_ok_at": None,
    })
    now = time.time()
    if error is None:
        entry["calls"] += 1
        entry["last_ok_at"] = now
    else:
        entry["errors"] += 1
        entry["last_error"] = error if isinstance(error, str) else type(error).__name__
        entry["last_error_at"] = now


def error_type(error_frame) -> str:
    """Pipecat ErrorFrame → a type name (exception class, else the error category)."""
    if getattr(error_frame, "exception", None) is not None:
        return type(error_frame.exception).__name__
    category = getattr(error_frame, "category", None)
    return f"ErrorFrame.{category.name}" if category is not None else "ErrorFrame"


def snapshot(active_calls: int) -> dict[str, Any]:
    now = time.time()
    st = live_settings.state
    return {
        "version": VERSION,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "started_at": round(STARTED_AT, 3),
        "uptime_secs": round(now - STARTED_AT),
        "active_calls": active_calls,
        "settings_version": st.settings_version,
        "secrets_version": st.secrets_version,
        "last_reload": st.last_reload or None,
        "providers": _providers,
        "updated_at": round(now, 3),
    }


async def publish(active_calls: int) -> None:
    await store.client().set(store.key("status"), json.dumps(snapshot(active_calls)), ex=TTL_SECS)


async def run(active_calls: Callable[[], int], interval: float = INTERVAL_SECS) -> None:
    while True:
        try:
            await publish(active_calls())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Status publish failed: {}", type(e).__name__)
        await asyncio.sleep(interval)
