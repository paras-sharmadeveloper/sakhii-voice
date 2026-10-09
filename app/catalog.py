"""GET /catalog: every provider this engine can run, for Laravel's admin.

Built from the provider registry (each provider file's INFO and optional
voices()). Voices are fetched live with the engine's .env keys and cached
for an hour; a provider without keys reports why instead of failing the
whole catalogue.
"""

import asyncio
import importlib
import time
from pathlib import Path

from loguru import logger

from app.providers import REGISTRY
from app.settings import Settings, get_settings

VOICES_TTL = 3600
_voice_cache: dict[str, tuple[float, list[dict]]] = {}


def env_credentials(provider: str, s: Settings) -> dict[str, str]:
    """The .env fallback credentials for a provider's voices API."""
    google_json = s.google_credentials_json
    if not google_json and s.google_credentials_path and Path(s.google_credentials_path).is_file():
        google_json = Path(s.google_credentials_path).read_text()
    creds = {
        "sarvam": {"api_key": s.sarvam_api_key},
        "elevenlabs": {"api_key": s.elevenlabs_api_key},
        "azure": {"api_key": s.azure_speech_key, "region": s.azure_speech_region},
        "google": {"credentials_json": google_json},
        "cartesia": {"api_key": s.cartesia_api_key},
        "deepgram": {"api_key": s.deepgram_api_key},
    }.get(provider, {})
    return {k: v for k, v in creds.items() if v}


async def _voices(provider: str, fetch) -> tuple[list[dict] | None, str | None]:
    cached = _voice_cache.get(provider)
    if cached and cached[0] > time.monotonic():
        return cached[1], None
    creds = env_credentials(provider, get_settings())
    try:
        voices = await asyncio.wait_for(fetch(creds), timeout=15)
    except KeyError:
        return None, "no credentials configured on the engine"
    except Exception as e:  # never echo request details: they can carry keys
        logger.warning("Voices for {} failed: {}", provider, type(e).__name__)
        return None, f"voices API failed ({type(e).__name__})"
    _voice_cache[provider] = (time.monotonic() + VOICES_TTL, voices)
    return voices, None


async def build_catalog() -> dict:
    out: dict = {"version": 1, "generated_at": int(time.time())}
    jobs = []
    for kind, providers in REGISTRY.items():
        entries = []
        for provider_id, path in providers.items():
            module = importlib.import_module(path.partition(":")[0])
            info = getattr(module, "INFO", None)
            if info is None:
                continue
            entry = {"id": provider_id, "kind": kind, **info.to_dict(), "voices": None, "voices_error": None}
            entries.append(entry)
            if kind == "tts" and hasattr(module, "voices"):
                jobs.append((entry, _voices(provider_id, module.voices)))
        out[kind] = entries
    results = await asyncio.gather(*(job for _, job in jobs))
    for (entry, _), (voices, error) in zip(jobs, results, strict=True):
        entry["voices"], entry["voices_error"] = voices, error
    return out


def clear_cache() -> None:
    _voice_cache.clear()
