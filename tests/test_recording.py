"""Call recording: stereo PCM → MP3 → S3-compatible storage."""

import os

import boto3
import pytest
from moto import mock_aws

from app import recording
from app.settings import get_settings

RATE = 8000


@pytest.fixture
def s3(monkeypatch):
    for k, v in {"RECORDING_S3_BUCKET": "calls", "RECORDING_S3_KEY": "AKIA", "RECORDING_S3_SECRET": "s",
                 "RECORDING_S3_REGION": "us-east-1", "RECORDING_S3_ENDPOINT": "",
                 "RECORDING_PUBLIC_BASE_URL": "https://cdn.example.com/"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="calls")
        yield client
    get_settings.cache_clear()


def _stereo(secs: float) -> bytes:
    return b"\x10\x00\xf0\xff" * int(RATE * secs)  # left/right 16-bit frames


def test_encode_mp3(tmp_path):
    raw, mp3 = tmp_path / "a.pcm", tmp_path / "a.mp3"
    raw.write_bytes(_stereo(2))
    recording.encode_mp3(str(raw), str(mp3), RATE, 32)
    data = mp3.read_bytes()
    assert data[:3] == b"ID3" or data[0] == 0xFF
    assert 4_000 < len(data) < 12_000  # ~32 kbps


async def test_finish_uploads_and_cleans_up(s3):
    assert recording.storage_configured(get_settings())
    rec = recording.CallRecorder("CA77", RATE)
    handler = rec.processor._event_handlers["on_audio_data"].handlers[0]
    for _ in range(3):  # three chunks, as the processor would deliver them
        await handler(rec.processor, _stereo(1), RATE, 2)

    result = await rec.finish()

    assert result is not None
    assert result.key.startswith("recordings/") and result.key.endswith("/CA77.mp3")
    assert result.url == f"https://cdn.example.com/{result.key}"
    assert result.duration_secs == 3.0
    obj = s3.get_object(Bucket="calls", Key=result.key)
    assert obj["ContentType"] == "audio/mpeg" and obj["ContentLength"] > 0
    assert not os.path.exists(rec.raw_path) and not os.path.exists(rec.raw_path[:-4] + ".mp3")


async def test_a_failed_upload_loses_only_the_recording(s3, monkeypatch):
    def broken(*_):
        raise RuntimeError("bucket gone")

    monkeypatch.setattr(recording, "upload", broken)
    rec = recording.CallRecorder("CA78", RATE)
    await rec.processor._event_handlers["on_audio_data"].handlers[0](rec.processor, _stereo(1), RATE, 2)
    assert await rec.finish() is None
    assert not os.path.exists(rec.raw_path)


async def test_silent_call_uploads_nothing(s3):
    rec = recording.CallRecorder("CA79", RATE)
    assert await rec.finish() is None
    assert s3.list_objects_v2(Bucket="calls").get("KeyCount") == 0
