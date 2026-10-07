"""Redis contract shared with Laravel.

Laravel (control plane) writes:
    sakhii:voice:agent:{agent_id}       STRING  agent config JSON (see agent_config.py)
    sakhii:voice:number:{e164}          STRING  agent_id answering that ExoPhone
    sakhii:voice:call:{call_sid}:init   STRING  optional, for outbound calls:
                                                {"agent_id": "...", "variables": {...}}

This engine writes:
    sakhii:voice:call:{call_sid}        HASH    live call state (TTL'd)
    sakhii:voice:active                 SET     call_sids currently on the engine
    sakhii:voice:events                 STREAM  call.started / call.ended events

All keys get REDIS_KEY_PREFIX in front so they line up with Laravel's prefix.
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as redis
from loguru import logger

from app.agent_config import AgentConfig
from app.settings import get_settings

_client: redis.Redis | None = None


def client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def key(*parts: str) -> str:
    return get_settings().redis_key_prefix + "sakhii:voice:" + ":".join(parts)


def normalize_number(raw: str | None) -> str | None:
    """Exotel sends numbers as 0XXXXXXXXXX, 91XXXXXXXXXX or +91XXXXXXXXXX."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "+91" + digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    return "+" + digits if digits else None


class AgentNotFound(Exception):
    pass


@dataclass
class ResolvedCall:
    agent: AgentConfig
    variables: dict[str, str] = field(default_factory=dict)


async def resolve_agent(
    call_sid: str, to_number: str | None, from_number: str | None, agent_id: str | None
) -> ResolvedCall:
    """Find the agent for this call in at most two Redis round trips.

    Order: explicit agent_id (Voicebot URL param) > outbound pre-seed for this
    call_sid > the dialled ExoPhone > the caller-id number (outbound flows
    where Exotel reports the ExoPhone as "from").
    """
    r = client()
    to_e164, from_e164 = normalize_number(to_number), normalize_number(from_number)
    async with r.pipeline(transaction=False) as p:
        p.get(key("call", call_sid, "init"))
        p.get(key("number", to_e164 or "-"))
        p.get(key("number", from_e164 or "-"))
        init_raw, by_to, by_from = await p.execute()

    init: dict[str, Any] = json.loads(init_raw) if init_raw else {}
    resolved_id = agent_id or init.get("agent_id") or by_to or by_from
    if not resolved_id:
        raise AgentNotFound(f"no agent for call {call_sid} (to={to_e164}, from={from_e164})")

    raw = await r.get(key("agent", str(resolved_id)))
    if not raw:
        raise AgentNotFound(f"agent {resolved_id} has no config in Redis")

    variables = {k: str(v) for k, v in (init.get("variables") or {}).items() if v is not None}
    return ResolvedCall(agent=AgentConfig.model_validate_json(raw), variables=variables)


async def call_started(call_sid: str, fields: dict[str, Any]) -> None:
    s = get_settings()
    r = client()
    state = {**fields, "status": "in_progress", "started_at": time.time()}
    try:
        async with r.pipeline(transaction=False) as p:
            p.hset(key("call", call_sid), mapping=_flat(state))
            p.expire(key("call", call_sid), s.call_state_ttl_secs)
            p.sadd(key("active"), call_sid)
            p.xadd(
                key("events"),
                {"type": "call.started", "call_sid": call_sid, "data": json.dumps(state)},
                maxlen=s.events_stream_maxlen,
                approximate=True,
            )
            await p.execute()
    except redis.RedisError as e:
        logger.warning("call.started write failed for {}: {}", call_sid, e)


async def call_ended(call_sid: str, summary: dict[str, Any]) -> None:
    s = get_settings()
    r = client()
    state = {**summary, "status": "completed", "ended_at": time.time()}
    try:
        async with r.pipeline(transaction=False) as p:
            p.hset(key("call", call_sid), mapping=_flat(state))
            p.expire(key("call", call_sid), s.call_state_ttl_secs)
            p.srem(key("active"), call_sid)
            p.xadd(
                key("events"),
                {"type": "call.ended", "call_sid": call_sid, "data": json.dumps(state)},
                maxlen=s.events_stream_maxlen,
                approximate=True,
            )
            await p.execute()
    except redis.RedisError as e:
        logger.error("call.ended write failed for {}: {}", call_sid, e)


async def set_call_fields(call_sid: str, fields: dict[str, Any]) -> None:
    try:
        await client().hset(key("call", call_sid), mapping=_flat(fields))
    except redis.RedisError as e:
        logger.warning("call state update failed for {}: {}", call_sid, e)


def _flat(d: dict[str, Any]) -> dict[str, str]:
    return {
        k: v if isinstance(v, str) else json.dumps(v)
        for k, v in d.items()
        if v is not None
    }
