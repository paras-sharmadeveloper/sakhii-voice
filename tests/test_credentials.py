"""Laravel-encrypted provider credentials (AES-256-GCM)."""

import base64
import json
import os
import shutil
import subprocess

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from loguru import logger

from app import credentials, store
from app.agent_config import AgentConfig
from app.settings import get_settings

KEY = base64.b64encode(os.urandom(32)).decode()
SECRET = "dg-live-SECRET-1234567890"


def encrypt(cred_id: str, fields: dict, key: str = KEY) -> str:
    """What Laravel writes, in Python."""
    iv = os.urandom(12)
    plain = json.dumps({"provider": "deepgram", "fields": fields}).encode()
    sealed = AESGCM(base64.b64decode(key)).encrypt(iv, plain, f"sakhii:voice:cred:{cred_id}".encode())
    return json.dumps({
        "v": 1, "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(sealed[:-16]).decode(), "tag": base64.b64encode(sealed[-16:]).decode(),
    })


@pytest.fixture(autouse=True)
def cred_key(monkeypatch):
    monkeypatch.setenv("SAKHII_VOICE_CRED_KEY", KEY)
    get_settings.cache_clear()
    credentials.clear_cache()
    yield
    get_settings.cache_clear()
    credentials.clear_cache()


def test_roundtrip():
    assert credentials.decrypt("12", encrypt("12", {"api_key": SECRET})) == {"api_key": SECRET}


PHP = r'''
$key = base64_decode(getenv("K"));
$id = "7";
$iv = random_bytes(12);
$plain = json_encode(["provider" => "deepgram", "fields" => ["api_key" => getenv("S")]]);
$ct = openssl_encrypt($plain, "aes-256-gcm", $key, OPENSSL_RAW_DATA, $iv, $tag, "sakhii:voice:cred:" . $id, 16);
echo json_encode(["v" => 1, "iv" => base64_encode($iv), "ct" => base64_encode($ct), "tag" => base64_encode($tag)]);
'''


@pytest.mark.skipif(not shutil.which("php"), reason="php not installed")
def test_a_value_encrypted_by_php_decrypts():
    out = subprocess.run(["php", "-r", PHP], env={**os.environ, "K": KEY, "S": SECRET},
                         capture_output=True, text=True, check=True).stdout
    assert credentials.decrypt("7", out) == {"api_key": SECRET}


@pytest.mark.parametrize("bad", [
    lambda: credentials.decrypt("13", encrypt("12", {"api_key": SECRET})),  # copied to another id
    lambda: credentials.decrypt("12", encrypt("12", {"api_key": SECRET}, base64.b64encode(os.urandom(32)).decode())),
    lambda: credentials.decrypt("12", "not json " + SECRET),
])
def test_tampering_fails_without_echoing_the_value(bad):
    with pytest.raises(credentials.CredentialError) as e:
        bad()
    assert SECRET not in str(e.value) and e.value.__cause__ is None


def test_rejects_a_short_key(monkeypatch):
    monkeypatch.setenv("SAKHII_VOICE_CRED_KEY", base64.b64encode(b"short").decode())
    get_settings.cache_clear()
    with pytest.raises(credentials.CredentialError, match="32 bytes"):
        credentials.decrypt("12", encrypt("12", {"api_key": SECRET}))


class FakeRedis:
    def __init__(self, values):
        self.values, self.gets = values, 0

    async def get(self, key):
        self.gets += 1
        return self.values.get(key)


async def test_for_agent_reads_once_caches_and_never_logs(monkeypatch, agent_dict):
    fake = FakeRedis({store.key("cred", "55"): encrypt("55", {"api_key": SECRET})})
    monkeypatch.setattr(store, "client", lambda: fake)
    agent_dict["models"]["stt"] = {"provider": "deepgram", "credential_id": "55"}
    agent_dict["models"]["tts"] = {"provider": "sarvam", "credential_id": "55"}
    agent = AgentConfig.model_validate(agent_dict)

    lines: list[str] = []
    sink = logger.add(lines.append, level="TRACE")
    try:
        first = await credentials.for_agent(agent)
        second = await credentials.for_agent(agent)
    finally:
        logger.remove(sink)

    assert first == second == {"stt": {"api_key": SECRET}, "tts": {"api_key": SECRET}}
    assert fake.gets == 1
    assert not any(SECRET in line for line in lines)


async def test_missing_credential(monkeypatch, agent_dict):
    monkeypatch.setattr(store, "client", lambda: FakeRedis({}))
    agent_dict["models"]["llm"]["credential_id"] = "404"
    with pytest.raises(credentials.CredentialError, match="404 not found"):
        await credentials.for_agent(AgentConfig.model_validate(agent_dict))
