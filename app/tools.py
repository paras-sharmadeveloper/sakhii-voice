"""Functions the LLM can call during a call.

Built-ins: end_call, transfer_call (when enabled), search_knowledge (when the
agent has a document search endpoint). Plus one HTTP tool per entry in the
agent's Integrations tab.

Ending and transferring both work by speaking a last line and then closing
the Exotel stream. Exotel then moves to the next applet in the flow; for a
transfer that applet asks Laravel what to do, and Laravel reads
`next_action` / `transfer_to` from the call state we write here first.
"""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import EndWorkerFrame, FunctionCallResultProperties, TTSSpeakFrame
from pipecat.services.llm_service import FunctionCallParams, LLMService

from app import store
from app.agent_config import AgentConfig, HttpTool
from app.prompt import render

MAX_TOOL_RESULT_CHARS = 4_000

_DEFAULT_TRANSFER_LINE = {
    "hi-IN": "Ek moment, main aapko hamari team se connect kar rahi hoon.",
}
_DEFAULT_TRANSFER_LINE_EN = "One moment, I'm connecting you to our team."
_DEFAULT_GOODBYE = {"hi-IN": "Dhanyavaad. Aapka din shubh ho."}
_DEFAULT_GOODBYE_EN = "Thank you for calling. Have a good day."


@dataclass
class CallSession:
    call_sid: str
    agent: AgentConfig
    variables: dict[str, str]
    from_number: str | None = None
    to_number: str | None = None
    end_reason: str | None = None
    transfer_to: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


# One pooled client for the whole process: no TLS handshake per tool call.
_http: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(
            timeout=8.0,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            headers={"User-Agent": "sakhii-voice/1"},
        )
    return _http


async def close_http() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


def _no_followup() -> FunctionCallResultProperties:
    return FunctionCallResultProperties(run_llm=False)


async def _say_and_hang_up(params: FunctionCallParams, line: str) -> None:
    if line:
        await params.llm.push_frame(TTSSpeakFrame(line))
    # Downstream, behind the line above, so the caller hears it in full first.
    await params.llm.push_frame(EndWorkerFrame(reason="agent_hangup"))


def register(llm: LLMService, session: CallSession) -> ToolsSchema:
    agent = session.agent
    lang = agent.languages.primary
    schemas: list[FunctionSchema] = []

    async def end_call(params: FunctionCallParams):
        session.end_reason = "agent_ended"
        await params.result_callback({"status": "ending"}, properties=_no_followup())
        closing = render(agent.greeting.closing, session.variables) or _DEFAULT_GOODBYE.get(
            lang, _DEFAULT_GOODBYE_EN
        )
        await _say_and_hang_up(params, closing)

    llm.register_function("end_call", end_call, cancel_on_interruption=False)
    schemas.append(
        FunctionSchema(
            name="end_call",
            description="End the call politely once the conversation is finished.",
            properties={},
            required=[],
        )
    )

    if agent.transfer.enabled and agent.transfer.number:

        async def transfer_call(params: FunctionCallParams):
            session.end_reason = "transferred"
            session.transfer_to = agent.transfer.number
            # Must land before the stream closes: Exotel's next applet reads it.
            await store.set_call_fields(
                session.call_sid,
                {"next_action": "transfer", "transfer_to": agent.transfer.number},
            )
            await params.result_callback({"status": "transferring"}, properties=_no_followup())
            line = render(agent.transfer.message, session.variables) or _DEFAULT_TRANSFER_LINE.get(
                lang, _DEFAULT_TRANSFER_LINE_EN
            )
            await _say_and_hang_up(params, line)

        llm.register_function("transfer_call", transfer_call, cancel_on_interruption=False)
        schemas.append(
            FunctionSchema(
                name="transfer_call",
                description=(
                    "Transfer the caller to a human team member. Use when they ask for a "
                    "person, raise a dispute, or you cannot help."
                ),
                properties={"reason": {"type": "string", "description": "Why, in a few words."}},
                required=[],
            )
        )

    if agent.knowledge.search_url:
        search_url = agent.knowledge.search_url

        async def search_knowledge(params: FunctionCallParams):
            body = {
                "agent_id": agent.agent_id,
                "call_sid": session.call_sid,
                "query": params.arguments.get("query", ""),
            }
            await params.result_callback(await _http_call("POST", search_url, body, {}, 3.0))

        llm.register_function("search_knowledge", search_knowledge)
        schemas.append(
            FunctionSchema(
                name="search_knowledge",
                description=(
                    "Search the business's documents. Only use it when the caller asks "
                    "something the FAQs and details above don't answer."
                ),
                properties={"query": {"type": "string", "description": "What to look up."}},
                required=["query"],
            )
        )

    for tool in agent.tools:
        llm.register_function(tool.name, _http_tool_handler(tool, session))
        params_schema = tool.parameters or {}
        schemas.append(
            FunctionSchema(
                name=tool.name,
                description=tool.description,
                properties=params_schema.get("properties", {}),
                required=params_schema.get("required", []),
            )
        )

    return ToolsSchema(standard_tools=schemas)


def _http_tool_handler(tool: HttpTool, session: CallSession):
    async def handler(params: FunctionCallParams):
        if tool.wait_message:
            await params.llm.push_frame(TTSSpeakFrame(render(tool.wait_message, session.variables)))
        body = {
            **params.arguments,
            "_call": {
                "call_sid": session.call_sid,
                "agent_id": session.agent.agent_id,
                "from": session.from_number,
                "to": session.to_number,
            },
        }
        result = await _http_call(tool.method, tool.url, body, tool.headers, tool.timeout_secs)
        session.tool_calls.append({"name": tool.name, "arguments": params.arguments})
        await params.result_callback(result)

    return handler


async def _http_call(
    method: str, url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> dict[str, Any]:
    method = method.upper()
    try:
        if method == "GET":
            flat = {k: v for k, v in body.items() if not isinstance(v, (dict, list))}
            resp = await http().get(url, params=flat, headers=headers, timeout=timeout)
        else:
            resp = await http().request(method, url, json=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        logger.warning("Tool call to {} failed: {}", url, e)
        return {"error": "The system is not reachable right now."}

    text = resp.text[:MAX_TOOL_RESULT_CHARS]
    if resp.status_code >= 400:
        logger.warning("Tool call to {} returned {}: {}", url, resp.status_code, text[:200])
        return {"error": f"Request failed ({resp.status_code})."}
    try:
        return {"result": json.loads(text)}
    except ValueError:
        return {"result": text}
