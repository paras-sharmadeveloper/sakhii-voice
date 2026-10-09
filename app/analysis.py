"""After-call analysis: one LLM request once the call is over.

Produces a short summary (English, plus the call's language when it isn't
English), an outcome from the agent's list, the caller's sentiment and the
fields the agent asks for, and emits it as a call.analyzed event. Runs as a
background task after call.ended; any failure is logged and dropped.
"""

import asyncio
import json

from loguru import logger

from app import store
from app.agent_config import LANGUAGE_NAMES, AgentConfig
from app.settings import get_settings

SENTIMENTS = ["positive", "neutral", "negative"]
TIMEOUT_SECS = 30
_tasks: set[asyncio.Task] = set()


def _prompt(agent: AgentConfig, language: str) -> str:
    fields = "\n".join(f'- "{f.key}": {f.description or f.key}' for f in agent.analysis.fields) or "(none)"
    local = LANGUAGE_NAMES.get(language, language)
    return f"""You analyse finished phone calls handled by a voice agent for {agent.business.name or 'a business'}.
Reply with one JSON object and nothing else:
{{
  "summary": "2-3 short lines in English: who called, what they wanted, what was agreed or what happens next",
  "summary_local": {'"the same summary in ' + local + '"' if not language.startswith("en") else "null"},
  "outcome": one of {json.dumps(agent.analysis.outcomes)},
  "sentiment": one of {json.dumps(SENTIMENTS)},
  "fields": {{ each key below: the value from the call, or null if it wasn't mentioned }}
}}
Fields to extract:
{fields}
Use only what was said in the call. Don't invent names, amounts or dates."""


def _transcript_text(transcript: list[dict]) -> str:
    return "\n".join(f"{'Caller' if t['role'] == 'user' else 'Agent'}: {t['text']}" for t in transcript)


def _clean(raw: dict, agent: AgentConfig, language: str) -> dict:
    outcome = raw.get("outcome")
    sentiment = raw.get("sentiment")
    fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
    return {
        "summary": str(raw.get("summary") or "").strip(),
        "summary_local": (str(raw["summary_local"]).strip() or None)
        if raw.get("summary_local") and not language.startswith("en") else None,
        "language": language,
        "outcome": outcome if outcome in agent.analysis.outcomes else None,
        "sentiment": sentiment if sentiment in SENTIMENTS else None,
        "fields": {f.key: fields.get(f.key) for f in agent.analysis.fields},
    }


async def analyze(agent: AgentConfig, transcript: list[dict], language: str, client=None) -> dict | None:
    s = get_settings()
    if not s.analysis_enabled or not agent.analysis.enabled or (not s.openai_api_key and client is None):
        return None
    if not any(t["role"] == "user" for t in transcript):
        return None  # nothing to analyse; Laravel marks these as missed
    if client is None:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=s.openai_api_key)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=s.analysis_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _prompt(agent, language)},
                {"role": "user", "content": _transcript_text(transcript)},
            ],
        ),
        timeout=TIMEOUT_SECS,
    )
    return _clean(json.loads(response.choices[0].message.content or "{}"), agent, language)


async def analyze_and_emit(call_sid: str, agent: AgentConfig, transcript: list[dict], language: str) -> None:
    try:
        result = await analyze(agent, transcript, language)
        if result:
            await store.call_analyzed(call_sid, {"agent_id": agent.agent_id, **result})
    except Exception as e:
        logger.warning("Analysis for {} failed: {}", call_sid, type(e).__name__)


def schedule(call_sid: str, agent: AgentConfig, transcript: list[dict], language: str) -> None:
    """Fire and forget, outside the call path."""
    task = asyncio.create_task(analyze_and_emit(call_sid, agent, transcript, language))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
