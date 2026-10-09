"""Call recording for agents with recording_enabled.

Stereo, caller on the left channel and the agent on the right, at the
telephony rate. During the call, audio is appended to a temp file in
10-second chunks off the event loop, so nothing waits on disk. After hang-up
it's encoded to MP3, uploaded to S3-compatible storage (S3, DigitalOcean
Spaces, Cloudflare R2, MinIO) and the temp files are deleted.
"""

import asyncio
import os
import tempfile
import time
from dataclasses import dataclass

from loguru import logger
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor

from app.settings import Settings, get_settings

CHANNELS = 2
SAMPLE_BYTES = 2
CHUNK_SECS = 10


@dataclass
class Recording:
    key: str
    url: str | None
    duration_secs: float


def storage_configured(s: Settings) -> bool:
    return bool(s.recording_s3_bucket and s.recording_s3_key and s.recording_s3_secret)


class CallRecorder:
    def __init__(self, call_sid: str, sample_rate: int):
        self.call_sid = call_sid
        self.sample_rate = sample_rate
        fd, self.raw_path = tempfile.mkstemp(prefix="sakhii-rec-", suffix=".pcm")
        os.close(fd)
        self._last_write: asyncio.Future | None = None
        self.processor = AudioBufferProcessor(
            sample_rate=sample_rate,
            num_channels=CHANNELS,
            buffer_size=sample_rate * SAMPLE_BYTES * CHANNELS * CHUNK_SECS,
            auto_start_recording=True,
        )

        @self.processor.event_handler("on_audio_data")
        async def _on_audio(_proc, audio: bytes, _rate: int, _channels: int):
            if audio:
                # Pipecat runs each handler call in its own task; chaining keeps chunks in order.
                self._last_write = asyncio.ensure_future(self._append_after(self._last_write, audio))

    async def _append_after(self, previous: asyncio.Future | None, audio: bytes) -> None:
        if previous is not None:
            await previous
        await asyncio.to_thread(self._append, audio)

    def _append(self, audio: bytes) -> None:
        with open(self.raw_path, "ab") as f:
            f.write(audio)

    async def finish(self) -> Recording | None:
        """Encode, upload, clean up. Never raises: a failed upload loses the recording, not the call log."""
        mp3_path = self.raw_path[:-4] + ".mp3"
        try:
            await self.processor.stop_recording()
            await asyncio.sleep(0)  # let the final flush's handler task start
            if self._last_write is not None:
                await self._last_write
            size = os.path.getsize(self.raw_path)
            if size == 0:
                return None
            duration = round(size / (self.sample_rate * SAMPLE_BYTES * CHANNELS), 1)
            s = get_settings()
            await asyncio.to_thread(encode_mp3, self.raw_path, mp3_path, self.sample_rate, s.recording_mp3_kbps)
            key = f"{s.recording_s3_prefix}{time.strftime('%Y/%m/%d')}/{self.call_sid}.mp3"
            await asyncio.to_thread(upload, mp3_path, key, s)
            url = f"{s.recording_public_base_url.rstrip('/')}/{key}" if s.recording_public_base_url else None
            return Recording(key=key, url=url, duration_secs=duration)
        except Exception as e:
            logger.error("Recording for {} failed: {}", self.call_sid, type(e).__name__)
            return None
        finally:
            for path in (self.raw_path, mp3_path):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass


def encode_mp3(raw_path: str, mp3_path: str, sample_rate: int, kbps: int) -> None:
    import lameenc

    encoder = lameenc.Encoder()
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(CHANNELS)
    encoder.set_bit_rate(kbps)
    encoder.set_quality(5)
    with open(raw_path, "rb") as src, open(mp3_path, "wb") as dst:
        while chunk := src.read(sample_rate * SAMPLE_BYTES * CHANNELS * 30):
            dst.write(encoder.encode(chunk))
        dst.write(encoder.flush())


def upload(path: str, key: str, s: Settings) -> None:
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=s.recording_s3_endpoint or None,
        aws_access_key_id=s.recording_s3_key,
        aws_secret_access_key=s.recording_s3_secret,
        region_name=s.recording_s3_region or None,
    )
    client.upload_file(path, s.recording_s3_bucket, key, ExtraArgs={"ContentType": "audio/mpeg"})
