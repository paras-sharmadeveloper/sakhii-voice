"""Runtime settings from Redis, hot-reloaded.

    sakhii:voice:settings   {"version": 7, "values": {"VAD_STOP_SECS": 0.25, ...}}
    sakhii:voice:secrets    AES-256-GCM envelope (same as credentials), additional
                            data "sakhii:voice:secrets", plaintext
                            {"version": 3, "fields": {"OPENAI_API_KEY": "...", ...}}
    sakhii:voice:settings:changed   pub/sub: any message = reload now

Reloads on a message and every POLL_SECS anyway. A config that doesn't
validate is rejected as a whole: the old one stays, and a settings.rejected
event says why (setting names and rules only, never values). Calls that are
already running keep the snapshot they started with (settings.pinned).
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pydantic import ValidationError

from app import credentials, store
from app.settings import Settings, apply, get_settings, names

POLL_SECS = 60
SECRETS_AAD = "sakhii:voice:secrets"


class ConfigRejected(Exception):
    """The reason is safe to log and publish: names and rules, no values."""


@dataclass
class State:
    settings_version: str | None = None  # None = nothing in Redis, .env only
    secrets_version: str | None = None
    fingerprint: str | None = None
    last_reload: dict[str, Any] = field(default_factory=dict)


state = State()
_on_change: list[Callable[[Settings], None]] = []


def on_change(fn: Callable[[Settings], None]) -> None:
    _on_change.append(fn)


def _keys(values: Any, store_name: str) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ConfigRejected(f"{store_name}: values must be a JSON object")
    allowed = names("runtime") if store_name == "settings" else names("secret")
    out: dict[str, Any] = {}
    for raw_name, value in values.items():
        name = str(raw_name).lower()
        if name not in Settings.model_fields:
            raise ConfigRejected(f"{store_name}: unknown setting {str(raw_name).upper()}")
        if name not in allowed:
            where = {"bootstrap": "the server's .env only", "secret": "sakhii:voice:secrets",
                     "runtime": "sakhii:voice:settings"}[Settings.kind(name)]
            raise ConfigRejected(f"{store_name}: {name.upper()} belongs in {where}")
        if value is None or value == "":
            continue  # not set here: .env value or default
        out[name] = value
    return out


def parse_settings(raw: str | None) -> tuple[dict[str, Any], str | None]:
    if not raw:
        return {}, None
    try:
        doc = json.loads(raw)
    except ValueError:
        raise ConfigRejected("settings: not valid JSON") from None
    if not isinstance(doc, dict):
        raise ConfigRejected("settings: must be a JSON object")
    version = doc.get("version")
    return _keys(doc.get("values", {}), "settings"), None if version is None else str(version)


def parse_secrets(raw: str | None) -> tuple[dict[str, Any], str | None]:
    if not raw:
        return {}, None
    try:
        doc = credentials.open_envelope(SECRETS_AAD, raw)
    except credentials.CredentialError as e:
        raise ConfigRejected(f"secrets: {e}") from None
    except ValueError:
        raise ConfigRejected("secrets: can't be decrypted (wrong SAKHII_VOICE_CRED_KEY or a damaged value)") from None
    version = doc.get("version")
    return _keys(doc.get("fields", {}), "secrets"), None if version is None else str(version)


def build(values: dict[str, Any], secrets: dict[str, Any]) -> Settings:
    """Redis values over .env over defaults. Raises ConfigRejected."""
    try:
        return Settings(**values, **secrets)
    except ValidationError as e:
        problems = []
        for err in e.errors(include_input=False, include_url=False, include_context=False):
            loc = ".".join(str(p) for p in err["loc"])
            problems.append(f"{loc.upper()}: {err['msg']}")
        raise ConfigRejected("; ".join(problems)) from None


def _changed(old: Settings, new: Settings) -> list[str]:
    return sorted(n.upper() for n in Settings.model_fields if getattr(old, n) != getattr(new, n))


async def reload(*, force: bool = False) -> bool | None:
    """True = applied, False = rejected, None = nothing changed."""
    raw_settings, raw_secrets = await store.client().mget(store.key("settings"), store.key("secrets"))
    fingerprint = hashlib.sha256(f"{raw_settings}\0{raw_secrets}".encode()).hexdigest()
    if fingerprint == state.fingerprint and not force:
        return None
    state.fingerprint = fingerprint  # a rejected config is reported once, not every poll
    now = time.time()
    try:
        values, settings_version = parse_settings(raw_settings)
        secrets, secrets_version = parse_secrets(raw_secrets)
        new = build(values, secrets)
    except ConfigRejected as e:
        reason = str(e)
        state.last_reload = {"at": now, "result": "rejected", "reason": reason}
        logger.warning("Settings rejected, keeping the current ones: {}", reason)
        await store.event("settings.rejected", {
            "reason": reason, "settings_version": _version(raw_settings), "at": now,
            "settings_version_in_use": state.settings_version, "secrets_version_in_use": state.secrets_version,
        })
        return False

    old = get_settings()
    changed = _changed(old, new)
    apply(new)
    state.settings_version = settings_version or (fingerprint[:12] if raw_settings else None)
    state.secrets_version = secrets_version or (fingerprint[12:24] if raw_secrets else None)
    state.last_reload = {"at": now, "result": "applied", "reason": None}
    logger.info(
        "Settings applied (settings {}, secrets {}); changed: {}",
        state.settings_version or ".env", state.secrets_version or ".env", ", ".join(changed) or "nothing",
    )  # names only, never values
    for fn in _on_change:
        try:
            fn(new)
        except Exception as e:
            logger.warning("Settings hook failed: {}", type(e).__name__)
    return True


def _version(raw: str | None) -> str | None:
    try:
        v = json.loads(raw or "null").get("version")
        return None if v is None else str(v)
    except (ValueError, AttributeError):
        return None


def channels() -> list[str]:
    """Prefixed and plain: phpredis/predis may or may not prefix PUBLISH channels."""
    prefixed = store.key("settings", "changed")
    plain = "sakhii:voice:settings:changed"
    return [prefixed] if prefixed == plain else [prefixed, plain]


async def _reload_safely() -> None:
    try:
        await reload()
    except Exception as e:  # Redis down etc.: keep the current settings
        logger.warning("Settings reload failed: {}", type(e).__name__)


async def watch(poll_secs: float = POLL_SECS) -> None:
    """Reload on pub/sub messages, and every poll_secs regardless."""
    while True:
        pubsub = store.client().pubsub()
        try:
            await pubsub.subscribe(*channels())
            next_poll = time.monotonic() + poll_secs
            while True:
                timeout = max(0.0, next_poll - time.monotonic())
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
                if message is not None or time.monotonic() >= next_poll:
                    await _reload_safely()
                    next_poll = time.monotonic() + poll_secs
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Settings watcher lost Redis ({}); retrying", type(e).__name__)
            await _reload_safely()
            await asyncio.sleep(min(5.0, poll_secs))
        finally:
            try:
                await pubsub.aclose()
            except Exception:
                pass
