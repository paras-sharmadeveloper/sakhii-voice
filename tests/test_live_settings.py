"""Runtime settings from Redis: precedence, hot reload, rejection, pinning, status."""

import asyncio
import base64
import json
import os

import pytest
import redis.asyncio as redis
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from loguru import logger

from app import live_settings, status, store
from app.settings import Settings, get_settings, pinned

PREFIX = "sakhii-test-"
REDIS_URL = "redis://127.0.0.1:6379/15"
KEY = base64.b64encode(os.urandom(32)).decode()
SECRET = "sk-live-NEVER-LOG-ME-123"


def seal(doc: dict, key: str = KEY, aad: str = "sakhii:voice:secrets") -> str:
    iv = os.urandom(12)
    sealed = AESGCM(base64.b64decode(key)).encrypt(iv, json.dumps(doc).encode(), aad.encode())
    return json.dumps({"v": 1, "iv": base64.b64encode(iv).decode(),
                       "ct": base64.b64encode(sealed[:-16]).decode(), "tag": base64.b64encode(sealed[-16:]).decode()})


@pytest.fixture
async def r(monkeypatch):
    for name in ("VAD_STOP_SECS", "OPENAI_API_KEY", "LOG_LEVEL", "TURN_DETECTION", "REDIS_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("REDIS_KEY_PREFIX", PREFIX)
    monkeypatch.setenv("SAKHII_VOICE_CRED_KEY", KEY)
    get_settings.cache_clear()
    await store.close()
    live_settings.state = live_settings.State()
    client = redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except redis.ConnectionError:
        pytest.skip("no local redis-server")
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()
    await store.close()
    get_settings.cache_clear()
    live_settings.state = live_settings.State()


async def put(r, values=None, secrets=None, version=1, secrets_version=1):
    if values is not None:
        await r.set(PREFIX + "sakhii:voice:settings", json.dumps({"version": version, "values": values}))
    if secrets is not None:
        await r.set(PREFIX + "sakhii:voice:secrets", seal({"version": secrets_version, "fields": secrets}))


async def events(r):
    return [(e[1]["type"], json.loads(e[1]["data"])) for e in await r.xrange(PREFIX + "sakhii:voice:events")]


# --- precedence ---------------------------------------------------------------

async def test_redis_over_env_over_default(r, monkeypatch):
    monkeypatch.setenv("VAD_STOP_SECS", "0.3")
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    get_settings.cache_clear()
    assert get_settings().vad_stop_secs == 0.3  # .env over default
    assert get_settings().vad_confidence == 0.7  # default

    await put(r, {"VAD_STOP_SECS": 0.45}, {"OPENAI_API_KEY": SECRET})
    assert await live_settings.reload() is True
    s = get_settings()
    assert s.vad_stop_secs == 0.45 and s.openai_api_key == SECRET  # Redis over .env
    assert s.vad_confidence == 0.7
    assert live_settings.state.settings_version == "1" and live_settings.state.secrets_version == "1"


async def test_nothing_in_redis_keeps_the_env(r, monkeypatch):
    monkeypatch.setenv("VAD_STOP_SECS", "0.3")
    get_settings.cache_clear()
    assert await live_settings.reload(force=True) is True
    assert get_settings().vad_stop_secs == 0.3
    assert live_settings.state.settings_version is None


async def test_empty_or_null_means_not_set(r, monkeypatch):
    """A blank admin field must never wipe a key that the .env still provides."""
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    get_settings.cache_clear()
    await put(r, {"VAD_STOP_SECS": None}, {"OPENAI_API_KEY": "", "EXOTEL_WS_TOKEN": None})
    assert await live_settings.reload() is True
    assert get_settings().openai_api_key == "from-env"


async def test_removing_a_value_from_redis_falls_back(r, monkeypatch):
    monkeypatch.setenv("VAD_STOP_SECS", "0.3")
    get_settings.cache_clear()
    await put(r, {"VAD_STOP_SECS": 0.5})
    await live_settings.reload()
    await put(r, {}, version=2)
    await live_settings.reload()
    assert get_settings().vad_stop_secs == 0.3


async def test_unchanged_config_is_not_reapplied(r):
    await put(r, {"VAD_STOP_SECS": 0.5})
    assert await live_settings.reload() is True
    assert await live_settings.reload() is None


# --- rejection ---------------------------------------------------------------

@pytest.mark.parametrize(("values", "secrets", "reason"), [
    ({"VAD_STOP_SECS": 9}, None, "VAD_STOP_SECS: Input should be less than or equal to 2"),
    ({"TURN_DETECTION": "sometimes"}, None, "TURN_DETECTION: Input should be 'smart' or 'off'"),
    ({"VAD_STOP_SECZ": 0.3}, None, "settings: unknown setting VAD_STOP_SECZ"),
    ({"OPENAI_API_KEY": SECRET}, None, "settings: OPENAI_API_KEY belongs in sakhii:voice:secrets"),
    ({"REDIS_KEY_PREFIX": "x"}, None, "settings: REDIS_KEY_PREFIX belongs in the server's .env only"),
    (None, {"VAD_STOP_SECS": 0.3}, "secrets: VAD_STOP_SECS belongs in sakhii:voice:settings"),
    (None, {"SAKHII_VOICE_CRED_KEY": SECRET}, "secrets: SAKHII_VOICE_CRED_KEY belongs in the server's .env only"),
])
async def test_invalid_config_is_rejected_and_the_old_one_kept(r, values, secrets, reason):
    await put(r, {"VAD_STOP_SECS": 0.5}, {"OPENAI_API_KEY": "good-key"})
    assert await live_settings.reload() is True
    before = get_settings()

    await put(r, values, secrets, version=2, secrets_version=2)
    lines: list[str] = []
    sink = logger.add(lines.append, level="TRACE")
    try:
        assert await live_settings.reload() is False
    finally:
        logger.remove(sink)

    assert get_settings() is before
    assert live_settings.state.settings_version == "1"
    assert live_settings.state.last_reload["result"] == "rejected"
    assert reason in live_settings.state.last_reload["reason"]
    rejected = [d for t, d in await events(r) if t == "settings.rejected"]
    assert len(rejected) == 1 and reason in rejected[0]["reason"]
    assert rejected[0]["settings_version_in_use"] == "1"
    raw = json.dumps(await events(r)) + "".join(lines)
    assert SECRET not in raw

    assert await live_settings.reload() is None  # reported once, not on every poll


async def test_secrets_sealed_with_another_key_are_rejected(r):
    await r.set(PREFIX + "sakhii:voice:secrets",
                seal({"fields": {"OPENAI_API_KEY": SECRET}}, key=base64.b64encode(os.urandom(32)).decode()))
    assert await live_settings.reload() is False
    assert "can't be decrypted" in live_settings.state.last_reload["reason"]


async def test_secrets_cannot_be_moved_from_another_key(r):
    """Additional data binds the envelope to sakhii:voice:secrets."""
    await r.set(PREFIX + "sakhii:voice:secrets", seal({"fields": {"OPENAI_API_KEY": SECRET}}, aad="sakhii:voice:cred:1"))
    assert await live_settings.reload() is False


async def test_bad_json_is_rejected(r):
    await r.set(PREFIX + "sakhii:voice:settings", "{not json")
    assert await live_settings.reload() is False
    assert live_settings.state.last_reload["reason"] == "settings: not valid JSON"


def test_choices_are_case_insensitive(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")  # older .env files
    assert Settings(turn_detection="Smart").log_level == "DEBUG"
    assert Settings(turn_detection="Smart").turn_detection == "smart"


async def test_secrets_never_show_in_repr():
    s = Settings(openai_api_key=SECRET, exotel_ws_token=SECRET, redis_password=SECRET)
    assert SECRET not in repr(s) and SECRET not in str(s)


# --- hot reload ---------------------------------------------------------------

async def _until(predicate, secs=5.0):
    for _ in range(int(secs / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


@pytest.mark.parametrize("channel", ["sakhii:voice:settings:changed", PREFIX + "sakhii:voice:settings:changed"])
async def test_pubsub_message_reloads(r, channel):
    watcher = asyncio.create_task(live_settings.watch(poll_secs=60))
    try:
        for _ in range(100):  # wait until the watcher has subscribed
            if (await r.pubsub_numsub(channel))[0][1]:
                break
            await asyncio.sleep(0.05)
        await put(r, {"VAD_STOP_SECS": 0.6}, version=5)
        await asyncio.sleep(0.1)
        await r.publish(channel, "5")
        await _until(lambda: live_settings.state.settings_version == "5")
        assert get_settings().vad_stop_secs == 0.6
    finally:
        watcher.cancel()


async def test_poll_picks_up_changes_without_a_message(r):
    watcher = asyncio.create_task(live_settings.watch(poll_secs=0.2))
    try:
        await put(r, {"LOG_LEVEL": "DEBUG"}, version=9)
        await _until(lambda: live_settings.state.settings_version == "9")
        assert get_settings().log_level == "DEBUG"
    finally:
        watcher.cancel()


async def test_hooks_run_on_change(r):
    seen = []
    live_settings.on_change(lambda s: seen.append(s.log_level))
    try:
        await put(r, {"LOG_LEVEL": "WARNING"})
        await live_settings.reload()
    finally:
        live_settings._on_change.pop()
    assert seen == ["WARNING"]


# --- live calls keep their settings --------------------------------------------

async def test_a_pinned_call_keeps_its_snapshot_across_tasks_and_threads(r):
    await put(r, {"VAD_STOP_SECS": 0.3})
    await live_settings.reload()
    seen = []
    changed = asyncio.Event()

    async def call():
        with pinned(get_settings()):
            async def in_a_task():
                await changed.wait()
                seen.append(get_settings().vad_stop_secs)
                seen.append(await asyncio.to_thread(lambda: get_settings().vad_stop_secs))
            await asyncio.create_task(in_a_task())
            seen.append(get_settings().vad_stop_secs)

    live = asyncio.create_task(call())
    await asyncio.sleep(0.05)
    await put(r, {"VAD_STOP_SECS": 0.9}, version=2)
    await live_settings.reload()
    changed.set()
    await live

    assert seen == [0.3, 0.3, 0.3]  # the call never saw the reload
    assert get_settings().vad_stop_secs == 0.9  # new calls do


# --- status ---------------------------------------------------------------------

async def test_status_is_republished_right_after_a_reload(r):
    """Laravel shows the reload result within seconds, not at the next 30 s tick."""
    live_settings.after_reload(lambda: status.publish(active_calls=0))
    try:
        await put(r, {"VAD_STOP_SECS": 0.3}, version=11)
        await live_settings.reload()
        assert json.loads(await r.get(PREFIX + "sakhii:voice:status"))["settings_version"] == "11"
        await put(r, {"VAD_STOP_SECS": 99}, version=12)
        await live_settings.reload()
        doc = json.loads(await r.get(PREFIX + "sakhii:voice:status"))
    finally:
        live_settings._after_reload.pop()
    assert doc["settings_version"] == "11" and doc["last_reload"]["result"] == "rejected"


async def test_status_is_published_without_secrets(r):
    await put(r, {"VAD_STOP_SECS": 0.3}, {"OPENAI_API_KEY": SECRET}, version=4, secrets_version=2)
    await live_settings.reload()
    status.record("tts", "cartesia", ConnectionError(f"refused with key {SECRET}"))
    status.record("tts", "cartesia")
    await status.publish(active_calls=3)

    raw = await r.get(PREFIX + "sakhii:voice:status")
    assert SECRET not in raw
    doc = json.loads(raw)
    assert doc["active_calls"] == 3 and doc["settings_version"] == "4" and doc["secrets_version"] == "2"
    assert doc["version"] and doc["uptime_secs"] >= 0
    assert doc["last_reload"]["result"] == "applied"
    cartesia = doc["providers"]["tts.cartesia"]
    assert cartesia["last_error"] == "ConnectionError" and cartesia["errors"] == 1 and cartesia["calls"] == 1
    assert 60 < await r.ttl(PREFIX + "sakhii:voice:status") <= 90


def test_git_version(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("0123456789abcdef0123\n")
    assert status.git_version(tmp_path) == "0123456789ab"
    (git / "refs" / "heads" / "main").unlink()
    (git / "packed-refs").write_text("# pack-refs\nfedcba9876543210ffff refs/heads/main\n")
    assert status.git_version(tmp_path) == "fedcba987654"


async def test_redis_host_settings(monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "10.0.0.5")
    monkeypatch.setenv("REDIS_PORT", "6380")
    monkeypatch.setenv("REDIS_DB", "2")
    monkeypatch.setenv("REDIS_PASSWORD", "p@ss/word")
    get_settings.cache_clear()
    await store.close()
    try:
        kw = store.client().connection_pool.connection_kwargs
        assert (kw["host"], kw["port"], kw["db"], kw["password"]) == ("10.0.0.5", 6380, 2, "p@ss/word")
    finally:
        await store.close()
        get_settings.cache_clear()
