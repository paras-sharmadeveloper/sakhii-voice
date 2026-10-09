"""Provider credentials written by Laravel, encrypted.

Key:   sakhii:voice:cred:<id>
Value: {"v": 1, "iv": b64(12 bytes), "ct": b64(ciphertext), "tag": b64(16 bytes)}
       AES-256-GCM, key = base64-decoded SAKHII_VOICE_CRED_KEY (32 bytes),
       additional data = "sakhii:voice:cred:<id>" (so a value can't be copied
       to another id).
Plaintext: {"provider": "deepgram", "fields": {"api_key": "…", …}}

Decrypted values live in memory only (cached 5 minutes) and are never logged.
"""

import base64
import json
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from loguru import logger

from app import store
from app.settings import get_settings

CACHE_SECS = 300
_cache: dict[str, tuple[float, dict[str, str]]] = {}


class CredentialError(Exception):
    """A referenced credential is missing or can't be decrypted. Carries the id only."""


def _key() -> bytes:
    raw = get_settings().sakhii_voice_cred_key
    try:
        key = base64.b64decode(raw, validate=True)
    except ValueError:
        key = b""
    if len(key) != 32:
        raise CredentialError("SAKHII_VOICE_CRED_KEY must be base64 of 32 bytes")
    return key


def open_envelope(aad: str, value: str) -> dict:
    """Decrypt {"v":1,"iv","ct","tag"} sealed with additional data `aad`; returns the plaintext JSON.

    Raises ValueError without any detail: the exception text could echo input.
    """
    key = _key()
    try:
        env = json.loads(value)
        plain = AESGCM(key).decrypt(
            base64.b64decode(env["iv"]),
            base64.b64decode(env["ct"]) + base64.b64decode(env["tag"]),
            aad.encode(),
        )
        data = json.loads(plain)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception:
        raise ValueError("can't be decrypted") from None


def decrypt(cred_id: str, value: str) -> dict[str, str]:
    try:
        fields = open_envelope(f"sakhii:voice:cred:{cred_id}", value)["fields"]
        return {str(k): str(v) for k, v in fields.items()}
    except CredentialError:
        raise
    except Exception:
        # Never include the value or the exception text (could echo input).
        raise CredentialError(f"credential {cred_id} can't be decrypted") from None


async def get(cred_id: str) -> dict[str, str]:
    now = time.monotonic()
    hit = _cache.get(cred_id)
    if hit and hit[0] > now:
        return hit[1]
    value = await store.client().get(store.key("cred", cred_id))
    if not value:
        raise CredentialError(f"credential {cred_id} not found")
    fields = decrypt(cred_id, value)
    _cache[cred_id] = (now + CACHE_SECS, fields)
    return fields


async def for_agent(agent) -> dict[str, dict[str, str]]:
    """{"stt": fields, "llm": fields, "tts": fields} for the choices that reference one."""
    out: dict[str, dict[str, str]] = {}
    for kind in ("stt", "llm", "tts"):
        cred_id = getattr(agent.models, kind).credential_id
        if cred_id:
            out[kind] = await get(cred_id)
            logger.debug("Using credential {} for {}", cred_id, kind)
    return out


def clear_cache() -> None:
    _cache.clear()
