"""GET /catalog for Laravel's admin."""

import httpx
import pytest

from app import catalog
from app.providers import tts_cartesia, tts_elevenlabs
from app.settings import get_settings


@pytest.fixture
async def http(monkeypatch):
    for name in ("ELEVENLABS_API_KEY", "CARTESIA_API_KEY", "AZURE_SPEECH_KEY", "DEEPGRAM_API_KEY",
                 "GOOGLE_CREDENTIALS_JSON", "GOOGLE_CREDENTIALS_PATH", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENGINE_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")
    get_settings.cache_clear()
    catalog.clear_cache()

    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield client
    get_settings.cache_clear()
    catalog.clear_cache()


AUTH = {"Authorization": "Bearer admin-secret"}


async def test_needs_the_admin_token(http, monkeypatch):
    assert (await http.get("/catalog")).status_code == 401
    assert (await http.get("/catalog", headers={"Authorization": "Bearer nope"})).status_code == 401
    monkeypatch.setenv("ENGINE_ADMIN_TOKEN", "")
    get_settings.cache_clear()
    assert (await http.get("/catalog", headers=AUTH)).status_code == 404  # off unless configured


async def test_lists_every_provider_with_live_voices_cached(http, monkeypatch):
    calls = []

    async def fake_voices(creds):
        calls.append(creds)
        return [{"id": "v1", "name": "Aria", "labels": {}, "preview_url": None}]

    monkeypatch.setattr(tts_elevenlabs, "voices", fake_voices)

    r = await http.get("/catalog", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1
    assert {p["id"] for p in body["stt"]} == {"sarvam", "elevenlabs", "deepgram", "google", "azure", "gladia", "assemblyai"}
    assert {p["id"] for p in body["llm"]} == {"openai", "sarvam", "gemini", "azure_openai", "groq", "anthropic"}

    tts = {p["id"]: p for p in body["tts"]}
    el = tts["elevenlabs"]
    assert el["voices"] == [{"id": "v1", "name": "Aria", "labels": {}, "preview_url": None}]
    assert el["voices_error"] is None
    assert set(el) >= {"name", "models", "languages", "credentials", "streaming", "tuning", "notes"}
    assert "api_key" in el["credentials"]
    assert tts["sarvam"]["voices"]  # static Bulbul list, no network
    # No keys on the engine: reported per provider, the rest of the catalogue still works.
    assert tts["cartesia"]["voices"] is None
    assert tts["cartesia"]["voices_error"] == "no credentials configured on the engine"
    assert "el-key" not in r.text

    await http.get("/catalog", headers=AUTH)
    assert calls == [{"api_key": "el-key"}]  # cached for an hour


async def test_a_failing_voices_api_reports_only_the_error_type(http, monkeypatch):
    async def broken(creds):
        raise httpx.ConnectError(f"refused, key={creds['api_key']}")

    monkeypatch.setenv("CARTESIA_API_KEY", "ca-secret")
    get_settings.cache_clear()
    monkeypatch.setattr(tts_cartesia, "voices", broken)
    r = await http.get("/catalog", headers=AUTH)
    entry = next(p for p in r.json()["tts"] if p["id"] == "cartesia")
    assert entry["voices_error"] == "voices API failed (ConnectError)"
    assert "ca-secret" not in r.text
