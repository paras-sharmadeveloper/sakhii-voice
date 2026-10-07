"""One live call: Exotel audio in -> STT -> LLM -> TTS -> Exotel audio out.

Latency decisions, in one place:
- Audio stays at 8 kHz end to end. STT is fed and TTS is asked for Exotel's
  native rate, so nothing resamples.
- Only streaming services (websocket STT and TTS, streamed LLM tokens).
- End of turn = Silero VAD (200 ms of silence) + the local Smart Turn model,
  instead of waiting a fixed ~0.6-1 s of silence. Same mechanism for every
  STT provider; the STT just finalises when we say the turn ended.
- The greeting goes straight to TTS; no LLM round trip before "Namaste".
- Agent lookup in Redis and the ONNX model loads run concurrently, off the
  event loop, while the caller is still hearing the ring/connect.
- Short replies are enforced by prompt and a max-token cap.
"""

import asyncio
import statistics
import time

from fastapi import WebSocket
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndWorkerFrame, TTSSpeakFrame
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import ExotelCallData
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from app import providers, store, turns
from app.exotel import ExotelSerializer, media_chunk_10ms_units
from app.prompt import PronunciationFilter, call_variables, render, system_prompt
from app.providers.base import CallContext
from app.settings import Settings, get_settings
from app.tools import CallSession, register

USER_IDLE_SECS = 8.0
_STILL_THERE = {"hi-IN": "Hello? Kya aap line par hain?"}
_STILL_THERE_EN = "Hello? Are you still there?"
_TIME_UP = {"hi-IN": "Hamara samay poora ho gaya hai. Dhanyavaad, aapka din shubh ho."}
_TIME_UP_EN = "We've reached the time limit for this call. Thank you, goodbye."


def _make_vad(s: Settings) -> SileroVADAnalyzer:
    return SileroVADAnalyzer(
        params=VADParams(
            confidence=s.vad_confidence, start_secs=s.vad_start_secs, stop_secs=s.vad_stop_secs
        )
    )


def warmup() -> None:
    """Load the ONNX runtimes once so the first call's sessions open fast."""
    s = get_settings()
    _make_vad(s)
    turns.stop_strategy(s)


async def run_call(websocket: WebSocket, call: ExotelCallData, agent_id: str | None) -> None:
    s = get_settings()
    call_sid = call.call_id or call.stream_id or "unknown"
    log = logger.bind(call_sid=call_sid)
    t0 = time.monotonic()

    try:
        resolved, vad, (turn_stop, smart_turn) = await asyncio.gather(
            store.resolve_agent(call_sid, call.to_number, call.from_number, agent_id),
            asyncio.to_thread(_make_vad, s),
            asyncio.to_thread(turns.stop_strategy, s),
        )
    except store.AgentNotFound as e:
        log.error("{}", e)
        await websocket.close(code=1011)
        return

    agent = resolved.agent
    variables = call_variables(agent, resolved.variables)
    session = CallSession(
        call_sid=call_sid,
        agent=agent,
        variables=variables,
        from_number=call.from_number,
        to_number=call.to_number,
    )
    pronunciation = PronunciationFilter(agent)
    ctx = CallContext(
        agent=agent,
        settings=s,
        sample_rate=s.exotel_sample_rate,
        text_filters=[pronunciation] if pronunciation.active else [],
    )

    try:
        stt = providers.build("stt", agent.models.stt, ctx)
        llm = providers.build("llm", agent.models.llm, ctx)
        tts = providers.build("tts", agent.models.tts, ctx)
    except Exception as e:
        log.exception("Agent {} has an unusable model config: {}", agent.agent_id, e)
        await websocket.close(code=1011)
        return

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            # Exotel's minimum media chunk; see app/exotel.py.
            audio_out_10ms_chunks=media_chunk_10ms_units(s.exotel_sample_rate),
            serializer=ExotelSerializer(
                stream_sid=call.stream_id or "",
                call_sid=call.call_id,
                params=ExotelSerializer.InputParams(exotel_sample_rate=s.exotel_sample_rate),
            ),
        ),
    )

    tools = register(llm, session)
    context = LLMContext(
        messages=[{"role": "system", "content": system_prompt(agent, variables)}],
        tools=tools,
    )
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad,
            user_turn_strategies=UserTurnStrategies(
                start=turns.start_strategies(s), stop=[turn_stop]
            ),
            user_idle_timeout=USER_IDLE_SECS,
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            aggregators.user(),
            llm,
            tts,
            transport.output(),
            aggregators.assistant(),
        ]
    )

    latency = UserBotLatencyObserver()
    turn_latencies: list[float] = []
    first_speech: list[float] = []

    @latency.event_handler("on_latency_measured")
    async def _on_latency(_obs, secs: float):
        turn_latencies.append(secs)
        log.info("voice-to-voice {:.0f} ms", secs * 1000)

    @latency.event_handler("on_first_bot_speech_latency")
    async def _on_first(_obs, secs: float):
        first_speech.append(secs)

    task = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=s.exotel_sample_rate,
            audio_out_sample_rate=s.exotel_sample_rate,
            enable_metrics=True,
        ),
        observers=[latency],
        enable_rtvi=False,
        idle_timeout_secs=120,
    )

    lang = agent.languages.primary
    idle_prompts = 0

    @aggregators.user().event_handler("on_user_turn_idle")
    async def _on_idle(_agg):
        nonlocal idle_prompts
        idle_prompts += 1
        if idle_prompts == 1:
            await task.queue_frame(TTSSpeakFrame(_STILL_THERE.get(lang, _STILL_THERE_EN)))
        else:
            session.end_reason = session.end_reason or "caller_silent"
            await task.queue_frame(EndWorkerFrame(reason="caller_silent"))

    @aggregators.user().event_handler("on_user_turn_started")
    async def _on_user_turn(_agg, *args):
        nonlocal idle_prompts
        idle_prompts = 0

    turn_ends: dict[str, int] = {}
    stop_timed_out = False

    @aggregators.user().event_handler("on_user_turn_stop_timeout")
    async def _on_stop_timeout(_agg, *args):
        nonlocal stop_timed_out
        stop_timed_out = True

    @aggregators.user().event_handler("on_user_turn_stopped")
    async def _on_user_turn_stopped(_agg, *args):
        nonlocal stop_timed_out
        if stop_timed_out:
            reason, prob = "stop_timeout", None
        elif smart_turn is not None:
            reason, prob = smart_turn.last_reason or turns.SMART_TURN, smart_turn.last_probability
            smart_turn.last_reason = smart_turn.last_probability = None
        else:
            reason, prob = turns.SILENCE_TIMEOUT, None
        stop_timed_out = False
        turn_ends[reason] = turn_ends.get(reason, 0) + 1
        log.info(
            "turn ended: reason={} p_complete={}",
            reason,
            f"{prob:.2f}" if prob is not None else "-",
        )

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport, _ws):
        greeting = render(agent.greeting.opening, variables)
        if greeting:
            await task.queue_frame(TTSSpeakFrame(greeting))
        log.info(
            "call up: agent={} stt={} llm={} tts={} setup={:.0f} ms",
            agent.agent_id,
            agent.models.stt.provider,
            agent.models.llm.provider,
            agent.models.tts.provider,
            (time.monotonic() - t0) * 1000,
        )

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport, _ws):
        session.end_reason = session.end_reason or "caller_hung_up"
        await task.cancel()

    max_secs = agent.max_call_duration_secs or s.default_max_call_secs

    async def _time_limit():
        await asyncio.sleep(max_secs)
        session.end_reason = "max_duration"
        await task.queue_frames(
            [TTSSpeakFrame(_TIME_UP.get(lang, _TIME_UP_EN)), EndWorkerFrame(reason="max_duration")]
        )

    started_wall = time.time()
    # Off the critical path, but must land before call_ended overwrites it.
    started_write = asyncio.create_task(
        store.call_started(
            call_sid,
            {
                "agent_id": agent.agent_id,
                "tenant_id": agent.tenant_id,
                "stream_sid": call.stream_id,
                "from": call.from_number,
                "to": call.to_number,
                "engine": "pipeline",
            },
        )
    )
    limiter = asyncio.create_task(_time_limit())
    try:
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(task)
        await runner.run()
    finally:
        limiter.cancel()
        await started_write
        await store.call_ended(
            call_sid,
            {
                "agent_id": agent.agent_id,
                "tenant_id": agent.tenant_id,
                "duration_secs": round(time.time() - started_wall, 1),
                "end_reason": session.end_reason or "agent_ended",
                "transfer_to": session.transfer_to,
                "latency_ms": _latency_summary(turn_latencies, first_speech),
                "turn_ends": turn_ends,
                "tool_calls": session.tool_calls,
                "transcript": _transcript(context),
            },
        )
        log.info("call over: {} ({})", session.end_reason, _latency_summary(turn_latencies, first_speech))


def _latency_summary(turns: list[float], first: list[float]) -> dict:
    ms = sorted(round(t * 1000) for t in turns)
    out: dict = {"turns": len(ms)}
    if first:
        out["greeting"] = round(first[0] * 1000)
    if ms:
        out["p50"] = round(statistics.median(ms))
        out["p90"] = ms[min(len(ms) - 1, int(len(ms) * 0.9))]
        out["max"] = ms[-1]
    return out


def _transcript(context: LLMContext) -> list[dict]:
    lines = []
    for m in context.get_messages():
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role not in ("user", "assistant") or not content:
            continue
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        lines.append({"role": role, "text": content})
    return lines
