"""Pretend to be Exotel and measure the engine's real latency with real providers.

    python scripts/latency_probe.py --url ws://127.0.0.1:8800/ws/exotel?token=XXX \
        --to 08047112233 --say question.wav [--say followup.wav] --out reply.wav

Each --say file must be 8 kHz mono 16-bit WAV with no trailing silence
(ffmpeg -i in.m4a -ar 8000 -ac 1 -sample_fmt s16 question.wav). The probe
waits for the greeting to finish, plays each file in real time, then reports
the time from the end of your audio to the first non-silent audio back.
This is the engine's share; the phone network adds roughly 150-250 ms more.
"""

import argparse
import array
import asyncio
import base64
import json
import time
import uuid
import wave

import websockets

CHUNK = 320  # 20 ms of 16-bit audio at 8 kHz
SILENCE = b"\x00" * CHUNK


def is_speech(pcm: bytes, threshold: int = 500) -> bool:
    samples = array.array("h", pcm)
    return bool(samples) and max(abs(s) for s in samples) > threshold


def load(path: str) -> bytes:
    with wave.open(path) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (8000, 1, 2):
            raise SystemExit(f"{path}: need 8 kHz mono 16-bit WAV")
        return w.readframes(w.getnframes())


async def main(args):
    sid, stream = "probe-" + uuid.uuid4().hex[:8], "ST-" + uuid.uuid4().hex[:8]
    out = bytearray()
    last_bot_audio = time.monotonic()
    first_speech_after: dict[str, float] = {}
    mark: dict[str, float] = {}

    async with websockets.connect(args.url) as ws:
        start = {"stream_sid": stream, "call_sid": sid, "account_sid": args.account,
                 "from": args.caller, "to": args.to, "custom_parameters": {}}
        t_start = time.monotonic()
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(json.dumps({"event": "start", "stream_sid": stream, "start": start}))

        async def reader():
            nonlocal last_bot_audio
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("event") != "media":
                    continue
                pcm = base64.b64decode(msg["media"]["payload"])
                out.extend(pcm)
                if is_speech(pcm):
                    now = time.monotonic()
                    last_bot_audio = now
                    for label, t in mark.items():
                        first_speech_after.setdefault(label, now - t)

        reader_task = asyncio.create_task(reader())
        mark["greeting"] = t_start

        async def send(pcm: bytes):
            for i in range(0, len(pcm), CHUNK):
                chunk = pcm[i:i + CHUNK].ljust(CHUNK, b"\x00")
                await ws.send(json.dumps({"event": "media", "stream_sid": stream,
                                          "media": {"payload": base64.b64encode(chunk).decode()}}))
                await asyncio.sleep(0.02)

        async def wait_quiet(secs: float, limit: float = 30):
            t0 = time.monotonic()
            while time.monotonic() - last_bot_audio < secs and time.monotonic() - t0 < limit:
                await send(SILENCE * 5)

        await send(SILENCE * 50)
        await wait_quiet(1.0)
        for n, path in enumerate(args.say, 1):
            await send(load(path))
            mark[f"reply {n}"] = time.monotonic()
            await send(SILENCE * 100)
            await wait_quiet(1.2)

        await ws.send(json.dumps({"event": "stop", "stream_sid": stream, "stop": {"call_sid": sid}}))
        reader_task.cancel()

    for label in mark:
        v = first_speech_after.get(label)
        print(f"{label:>10}: {v * 1000:.0f} ms" if v is not None else f"{label:>10}: no audio")
    if args.out:
        with wave.open(args.out, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000)
            w.writeframes(bytes(out))
        print("bot audio saved to", args.out)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--to", default="", help="ExoPhone the agent is mapped to")
    p.add_argument("--caller", default="09999999999")
    p.add_argument("--account", default="probe")
    p.add_argument("--say", action="append", default=[])
    p.add_argument("--out")
    asyncio.run(main(p.parse_args()))
