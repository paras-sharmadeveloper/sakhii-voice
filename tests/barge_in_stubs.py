"""Stand-ins for barge-in tests, registered like real providers.

- STT: segments on our VAD (like the real services finalise on it) and
  "transcribes" one word per 0.4 s of audio.
- LLM: the real OpenAILLMService pointed at a fake OpenAI-compatible server
  that streams slowly, so its real cancellation path is exercised.
- TTS: streams audio faster than real time in 100 ms pieces. Each reply's
  audio has its own constant amplitude, so the client can tell old audio from
  new: greeting 1000, first reply ("alpha") 2000, second ("bravo") 3000.
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pipecat.frames.frames import Frame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.settings import STTSettings, TTSSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601

MARKERS = ["alpha", "bravo", "charlie"]
AMPLITUDE = {"greeting": 1000, "alpha": 2000, "bravo": 3000, "charlie": 4000}
SECS_PER_WORD = 0.15


class State:
    llm_port = 0
    llm_requests: list[list[dict]] = []
    llm_cancelled: list[int] = []
    tts_started: list[tuple[float, str]] = []  # (monotonic time, marker)
    transcripts: list[str] = []

    @classmethod
    def reset(cls):
        cls.llm_requests, cls.llm_cancelled = [], []
        cls.tts_started, cls.transcripts = [], []


def marker_of(text: str) -> str:
    return next((m for m in MARKERS if m in text), "greeting")


# --- fake OpenAI ---------------------------------------------------------

llm_app = FastAPI()


@llm_app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    State.llm_requests.append(body["messages"])
    n = len(State.llm_requests)
    marker = MARKERS[n - 1]
    words = []
    for i in range(6):
        words += f"Reply {marker} sentence {i} about your loan account today.".split()

    async def stream():
        done = False
        try:
            for i, w in enumerate(words):
                chunk = {
                    "id": f"c{n}", "object": "chat.completion.chunk", "created": 0, "model": "gpt-4o-mini",
                    "choices": [{"index": 0, "delta": {"content": (" " if i else "") + w}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.08)
            end = {
                "id": f"c{n}", "object": "chat.completion.chunk", "created": 0, "model": "gpt-4o-mini",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(end)}\n\ndata: [DONE]\n\n"
            done = True
        finally:
            if not done:
                State.llm_cancelled.append(n)

    return StreamingResponse(stream(), media_type="text/event-stream")


# --- services --------------------------------------------------------------

class WordCountSTT(SegmentedSTTService):
    @property
    def wants_wav_segments(self) -> bool:
        return False

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        secs = len(audio) / 2 / self.sample_rate
        text = " ".join(["word"] * max(1, round(secs / 0.4)))
        State.transcripts.append(text)
        yield TranscriptionFrame(text, "caller", time_now_iso8601(), finalized=True)


class ToneTTS(TTSService):
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        marker = marker_of(text)
        State.tts_started.append((time.monotonic(), marker))
        piece = self.sample_rate // 10  # 100 ms
        sample = AMPLITUDE[marker].to_bytes(2, "little", signed=True)
        for _ in range(max(1, round(len(text.split()) * SECS_PER_WORD * 10))):
            yield TTSAudioRawFrame(sample * piece, self.sample_rate, 1, context_id=context_id)
            await asyncio.sleep(0.01)


def build_stt(choice, ctx):
    return WordCountSTT(
        sample_rate=ctx.sample_rate, trailing_silence_secs=0, settings=STTSettings(model=None, language=None)
    )


def build_llm(choice, ctx):
    return OpenAILLMService(
        api_key="test",
        base_url=f"http://127.0.0.1:{State.llm_port}/v1",
        settings=OpenAILLMService.Settings(model="gpt-4o-mini"),
    )


def build_tts(choice, ctx):
    return ToneTTS(
        sample_rate=ctx.sample_rate,
        push_start_frame=True,
        push_stop_frames=True,
        stop_frame_timeout_s=0.3,
        settings=TTSSettings(model=None, voice=None, language=None),
    )
