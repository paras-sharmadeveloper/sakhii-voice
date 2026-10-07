"""Exotel outbound media framing.

Exotel's rules for media we send back on a bidirectional (Voicebot) stream,
from https://support.exotel.com/support/solutions/articles/3000108630-working-with-the-stream-and-voicebot-applet
(article last updated 16 Sep 2025):

    "Media in the payloads are sent in raw/slin (16-bit, 8kHz, mono PCM
    (little-endian)) encoded in base64. The same is expected from the client
    in the case of bi directional streams"

    "Minimum chunk size: 3.2k [100ms data]"
    "Maximum chunk size: 100k"
    "Chunk size should always be in multiple of 320 bytes"
    "if the size X [for ex 4096] which is not in multiple of 320 Bytes: In
    this case, the last packet will be of lesser size than 320 Bytes, &
    platform will wait for 20ms before sending next chunk"

    Clear: {"event": "clear", "stream_sid": "<stream sid>"} clears "the audio
    data that was sent before but not yet played".

The doc contradicts itself on the minimum: 3.2k bytes is 200 ms at 8 kHz
16-bit mono, not 100 ms. We meet the stricter reading (3,200 bytes).

Pipecat's output transport already sends audio in fixed-size chunks and pads
the last chunk of each utterance with silence to a full chunk. We pick a
chunk size that satisfies the rules above (`media_chunk_10ms_units`), and the
serializer below enforces them anyway for any audio that reaches it some
other way.
"""

import math

from loguru import logger
from pipecat.frames.frames import AudioRawFrame, Frame, OutputAudioRawFrame
from pipecat.serializers.exotel import ExotelFrameSerializer

PCM_SAMPLE_BYTES = 2
CHUNK_MULTIPLE_BYTES = 320
MIN_CHUNK_BYTES = 3_200
MAX_CHUNK_BYTES = 100_000


def media_chunk_10ms_units(sample_rate: int) -> int:
    """Smallest whole number of 10 ms blocks whose byte size is >= the minimum
    chunk and a multiple of 320 bytes. Smallest = least audio left playing on
    Exotel's side when we send a clear. 8 kHz -> 20 (3,200 bytes = 200 ms)."""
    bytes_10ms = sample_rate // 100 * PCM_SAMPLE_BYTES
    step = math.lcm(bytes_10ms, CHUNK_MULTIPLE_BYTES) // bytes_10ms
    units = step
    while units * bytes_10ms < MIN_CHUNK_BYTES:
        units += step
    return units


def conform(pcm: bytes) -> bytes:
    """Pad with silence to a multiple of 320 bytes and at least the minimum."""
    size = max(MIN_CHUNK_BYTES, -(-len(pcm) // CHUNK_MULTIPLE_BYTES) * CHUNK_MULTIPLE_BYTES)
    return pcm + bytes(size - len(pcm))


class ExotelSerializer(ExotelFrameSerializer):
    """Pipecat's Exotel serializer, plus a guarantee that every media message
    meets Exotel's chunk rules."""

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, AudioRawFrame) and frame.sample_rate == self._exotel_sample_rate:
            audio = frame.audio
            if len(audio) % CHUNK_MULTIPLE_BYTES or len(audio) < MIN_CHUNK_BYTES:
                logger.debug("Padding {}-byte media chunk for Exotel", len(audio))
                audio = conform(audio)
            if len(audio) > MAX_CHUNK_BYTES:
                # Not reachable with our chunking (largest write is the 2 s
                # end-of-call silence, 32,000 bytes at 8 kHz), so be loud.
                logger.error("{}-byte media chunk exceeds Exotel's 100k limit", len(audio))
            if audio is not frame.audio:
                frame = OutputAudioRawFrame(audio=audio, sample_rate=frame.sample_rate, num_channels=1)
        return await super().serialize(frame)
