"""Turn-taking pieces: when the caller may interrupt, and why a turn ended."""

import asyncio

from loguru import logger
from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy, VADUserTurnStartStrategy
from pipecat.turns.user_stop import (
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)

from app.settings import Settings

# Why a user turn ended, as logged per turn.
SMART_TURN = "smart_turn"  # VAD silence, then the model said "finished"
SMART_TURN_SILENCE_FALLBACK = "smart_turn_silence_fallback"  # model said "not finished", silence ran out
SILENCE_TIMEOUT = "silence_timeout"  # smart turn off: fixed silence threshold


class BargeInGuardStartStrategy(VADUserTurnStartStrategy):
    """VAD turn start that is stricter while the bot is talking.

    With the bot silent, a turn starts on the VAD's speech start, as before, so
    a short "haan" still counts. While the bot is speaking, the caller has to
    keep talking for `min_speech_secs` in total before we interrupt; a cough,
    "hmm" or line noise that ends sooner is ignored.

    The VAD only reports "stopped" after `vad_stop_secs` of silence, so we can
    only be sure speech lasted `min_speech_secs` once that much longer has
    passed without a stop. Barge-in therefore lands about
    min_speech_secs + vad_stop_secs after the caller starts talking.
    """

    def __init__(self, *, min_speech_secs: float, vad_stop_secs: float, **kwargs):
        super().__init__(**kwargs)
        self._min_speech_secs = min_speech_secs
        self._vad_stop_secs = vad_stop_secs
        self._bot_speaking = False
        self._pending: asyncio.Task | None = None

    async def handle_user_turn_started(self):
        await self._cancel_pending()
        await super().handle_user_turn_started()

    async def cleanup(self):
        await self._cancel_pending()
        await super().cleanup()

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, VADUserStoppedSpeakingFrame) and self._pending:
            logger.info("Ignored a short sound while the bot was speaking (no interruption)")
            await self._cancel_pending()
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            if not self._bot_speaking:
                return await super().process_frame(frame)
            # The VAD already heard `start_secs` of speech before saying so.
            remaining = self._min_speech_secs - frame.start_secs
            if remaining <= 0:
                return await super().process_frame(frame)
            await self._cancel_pending()
            self._pending = self.task_manager.create_task(
                self._interrupt_after(remaining + self._vad_stop_secs), f"{self}::barge_in"
            )
            return ProcessFrameResult.STOP
        return ProcessFrameResult.CONTINUE

    async def _interrupt_after(self, delay: float):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._pending = None
        logger.info("Caller barged in")
        await self.trigger_user_turn_started()

    async def _cancel_pending(self):
        task, self._pending = self._pending, None
        if task and task is not asyncio.current_task():
            await self.task_manager.cancel_task(task)


class TracedSmartTurnAnalyzer(LocalSmartTurnAnalyzerV3):
    """Smart Turn v3 that remembers how it reached its last "turn complete"."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_reason: str | None = None
        self.last_probability: float | None = None
        self._silence_completed = False

    def append_audio(self, buffer: bytes, is_speech: bool) -> EndOfTurnState:
        state = super().append_audio(buffer, is_speech)
        if state == EndOfTurnState.COMPLETE:
            self._silence_completed = True
        return state

    async def analyze_end_of_turn(self):
        state, metrics = await super().analyze_end_of_turn()
        if self._silence_completed:
            self._silence_completed = False
            self.last_reason = SMART_TURN_SILENCE_FALLBACK
        elif state == EndOfTurnState.COMPLETE:
            self.last_reason = SMART_TURN
            self.last_probability = getattr(metrics, "probability", None)
        return state, metrics


def start_strategies(s: Settings) -> list:
    return [
        BargeInGuardStartStrategy(
            min_speech_secs=s.interrupt_min_speech_secs, vad_stop_secs=s.vad_stop_secs
        ),
        # Turns can also start from a transcript the VAD missed; while the bot
        # is talking that takes `interrupt_min_words` words.
        MinWordsUserTurnStartStrategy(min_words=s.interrupt_min_words),
    ]


def stop_strategy(s: Settings) -> tuple[object, TracedSmartTurnAnalyzer | None]:
    if s.turn_detection == "smart":
        analyzer = TracedSmartTurnAnalyzer(params=SmartTurnParams(stop_secs=s.smart_turn_stop_secs))
        return TurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer), analyzer
    return SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=s.user_speech_timeout), None
