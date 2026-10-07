"""Offline stand-ins for real providers, registered the same way a real
provider is (one registry line each). STT never hears anything, the LLM is a
real OpenAI service that is never invoked, TTS returns silence."""

from collections.abc import AsyncGenerator

from pipecat.frames.frames import Frame, TTSAudioRawFrame
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.settings import STTSettings, TTSSettings
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

SPOKEN: list[str] = []


class SilentSTT(STTService):
    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        yield None


class SilenceTTS(TTSService):
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        SPOKEN.append(text)
        # 300 ms of 16-bit mono silence at the requested rate.
        yield TTSAudioRawFrame(
            b"\x00\x00" * (self.sample_rate * 3 // 10), self.sample_rate, 1, context_id=context_id
        )


def build_stt(choice, ctx):
    return SilentSTT(sample_rate=ctx.sample_rate, settings=STTSettings(model=None, language=None))


def build_llm(choice, ctx):
    return OpenAILLMService(api_key="test", settings=OpenAILLMService.Settings(model="gpt-4o-mini"))


def build_tts(choice, ctx):
    return SilenceTTS(
        sample_rate=ctx.sample_rate,
        text_filters=ctx.text_filters,
        push_start_frame=True,
        push_stop_frames=True,
        stop_frame_timeout_s=0.2,
        settings=TTSSettings(model=None, voice=None, language=None),
    )
