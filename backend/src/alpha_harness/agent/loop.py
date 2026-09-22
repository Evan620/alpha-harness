"""Vision, the in-app agent: a streaming tool loop over every UI action, aware of the page.

The model sees five tools. Four only read; ``call_action`` reaches the 158 API operations
and always goes through :meth:`ApprovalGate.dispatch`, so a write becomes a proposal the
person approves in the panel rather than something the model does on its own.
"""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from fastapi import FastAPI

from ..llm.providers import get as provider_spec
from . import actions, guide
from .approval import ApprovalError, ApprovalGate
from .registry import AgentContext, UnknownCapability

log = structlog.get_logger(__name__)

MODEL = "glm-5.3"
MAX_ROUNDS = 16
HISTORY_LIMIT = 40
VISIBLE_CHARS = 3_500

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "explain_page",
            "description": "What a page of this app is for, what is on it and its key actions.",
            "parameters": {
                "type": "object",
                "properties": {"route": {"type": "string", "description": "e.g. /pool/submittable"}},
                "required": ["route"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_actions",
            "description": "Search every action the UI can perform (API operations) by keywords.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_action",
            "description": "Query parameters and JSON body schema for one action, before calling it.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_action",
            "description": (
                "Perform one action exactly as the UI would. Reads run immediately; anything "
                "that writes, simulates or spends LLM budget returns needs_approval and waits "
                "for the person to press Approve."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "object", "description": "Path parameters", "additionalProperties": {"type": "string"}},
                    "query": {"type": "object", "description": "Query-string parameters"},
                    "body": {"type": "object", "description": "JSON body, when the action takes one"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Move the person's screen to a page of the app so they can see it.",
            "parameters": {
                "type": "object",
                "properties": {"to": {"type": "string", "description": "An app route, e.g. /labs/search"}},
                "required": ["to"],
            },
        },
    },
]


BRAIN_ALPHA = "https://platform.worldquantbrain.com/alpha/"


def _system(context: dict[str, Any]) -> str:
    page = guide.page_for(context.get("pathname") or "/")
    here = (
        f"{page.title} ({page.route}): {page.summary}\n{page.detail}"
        if page
        else "An unmapped page."
    )
    visible = (context.get("visibleText") or "")[:VISIBLE_CHARS]
    scope = context.get("scope")
    return f"""You are Vision, the agent built into Alpha Harness, a local studio for WorldQuant BRAIN
research. You can do anything the person can do in this UI through tools, you can see the page
they are on, and your job is to make them effective on the platform fast.

HOW TO WORK
- Answer about the page they are on first; they are looking at it.
- To act: find_actions -> describe_action (for anything with a body) -> call_action.
- Reads run at once. Writes, simulations and LLM spends come back needs_approval: say in one line
  what you proposed and that it waits for their Approve click. Never claim it ran until it did.
- Human-only (sign-in, keys, update, quit) and blocked actions: say where they do it themselves.
- You never submit alphas to BRAIN.
- Use navigate when showing them a page helps. Only use numbers that came from a tool or from
  their screen; never invent one.

HOW TO WRITE (rendered as Markdown)
- Lead with the answer in one or two sentences, with the key number in **bold**.
- Then short sections or bullets. Keep it under ~180 words unless they ask for a tour or detail.
- Tables for comparisons of 3+ items (alphas, datasets, checks).
- LINK EVERYTHING you mention, never write a bare route:
  - app pages: [Data Explorer](/data), [Submittable](/pool/submittable), [Search Lab](/labs/search),
    [Tasks](/tasks), a task [Task 12](/tasks/12)
  - an alpha: [a1B2c3D](/alpha/a1B2c3D) in the app, and [on BRAIN]({BRAIN_ALPHA}a1B2c3D)
- Explain a BRAIN term (Sharpe, fitness, turnover, pyramid, Power Pool, near-miss) in a clause the
  first time only.
- End with at most one concrete next step, phrased as an offer.
- No em-dashes. No filler like "Great question".

THE PLATFORM (every page)
{guide.site_map()}

WHERE THEY ARE NOW
Route: {context.get("pathname") or "/"}   Page title: {context.get("title") or ""}
{here}
Scope in view: {json.dumps(scope) if scope else "unknown"}
What is on their screen (truncated):
---
{visible}
---"""


@dataclass
class Thread:
    id: int
    messages: list[dict[str, Any]] = field(default_factory=list)
    updated: float = field(default_factory=time.time)


class AgentService:
    def __init__(self, app: FastAPI, state: Any) -> None:
        self.app = app
        self.state = state
        self.registry, self.index = actions.build(app)
        self.registry.validate()
        self.gate = ApprovalGate(state, self.registry)
        self.threads: dict[int, Thread] = {}
        self._ids = itertools.count(1)

    # -- public ----------------------------------------------------------

    async def turn(
        self, thread_id: int | None, text: str, context: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        thread = self._thread(thread_id)
        thread.messages.append({"role": "user", "content": text})
        async for event in self._run(thread, context):
            yield event

    async def decide(
        self, proposal_id: str, payload_hash: str | None, approve: bool, context: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        proposal = next((p for p in self.gate.pending() if p.id == proposal_id), None)
        thread = self._thread(proposal.thread_id if proposal else None)
        try:
            if approve:
                yield {"type": "tool_start", "id": proposal_id, "tool": "approved",
                       "label": proposal.label if proposal else proposal_id, "args": {}}
                result = await self.gate.approve(proposal_id, payload_hash or "")
                outcome = json.dumps(result.to_dict(), default=str)
                yield {"type": "tool_end", "id": proposal_id, "status": result.status,
                       "httpStatus": _http_status(result.result), "preview": _preview(result.result)}
                note = f"[The person APPROVED {result.tool}. It ran. Result: {outcome[:4_000]}]"
            else:
                rejected = await self.gate.reject(proposal_id, reason="Rejected in the agent panel")
                note = f"[The person REJECTED {rejected.tool}. Do not retry it unless asked.]"
        except ApprovalError as exc:
            yield {"type": "error", "message": str(exc)}
            return
        thread.messages.append({"role": "user", "content": note + " Report the outcome briefly."})
        async for event in self._run(thread, context):
            yield event

    def catalog(self) -> list[dict[str, Any]]:
        return list(self.index.values())

    # -- loop ------------------------------------------------------------

    def _thread(self, thread_id: int | None) -> Thread:
        if thread_id is not None and thread_id in self.threads:
            return self.threads[thread_id]
        thread = Thread(id=next(self._ids))
        self.threads[thread.id] = thread
        return thread

    async def _run(self, thread: Thread, context: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "start", "threadId": thread.id}
        try:
            for _ in range(MAX_ROUNDS):
                message: dict[str, Any] = {}
                async for kind, value in self._complete(thread, context):
                    if kind == "message":
                        message = value
                    else:
                        yield {"type": kind, "delta": value}
                thread.messages.append(message)
                calls = message.get("tool_calls") or []
                if not calls:
                    break
                for call in calls:
                    fn = call.get("function") or {}
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    call_id = call.get("id", "")
                    yield {"type": "tool_start", "id": call_id, "tool": name, "args": args,
                           "label": self._label(name, args)}
                    output, step = await self._tool(thread, call_id, name, args, context)
                    yield {"type": "tool_end", "id": call_id, "status": step["status"],
                           "httpStatus": step.get("httpStatus"), "preview": _preview(output)}
                    if step.get("proposal"):
                        yield {"type": "proposal", "proposal": step["proposal"]}
                    if name == "navigate" and step["status"] == "ok":
                        yield {"type": "navigate", "to": str(args.get("to") or "")}
                    thread.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(output, default=str)[: actions.RESULT_CHARS + 500],
                        }
                    )
            else:
                thread.messages.append(
                    {"role": "user", "content": "[Step limit reached. Do not call tools. Answer now with what you have.]"}
                )
                message = {}
                async for kind, value in self._complete(thread, context, tools=False):
                    if kind == "message":
                        message = value
                    else:
                        yield {"type": kind, "delta": value}
                thread.messages.append(message)
        except Exception as exc:
            log.warning("agent.turn_failed", exc_info=True)
            yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
        finally:
            thread.messages = thread.messages[-HISTORY_LIMIT:]
            while thread.messages and thread.messages[0].get("role") == "tool":
                thread.messages.pop(0)
            thread.updated = time.time()
        yield {"type": "done", "threadId": thread.id}

    def _label(self, name: str, args: dict[str, Any]) -> str:
        if name == "call_action":
            entry = self.index.get(str(args.get("name") or ""))
            return f"{entry['method']} {entry['path']}" if entry else str(args.get("name") or "")
        if name == "find_actions":
            return f"search: {args.get('query', '')}"
        if name == "describe_action":
            return str(args.get("name") or "")
        if name in {"explain_page", "navigate"}:
            return str(args.get("route") or args.get("to") or "")
        return ""

    async def _tool(
        self, thread: Thread, call_id: str, name: str, args: dict[str, Any], context: dict[str, Any]
    ) -> tuple[Any, dict[str, Any]]:
        step: dict[str, Any] = {"tool": name, "args": args, "status": "ok"}
        try:
            if name == "explain_page":
                return guide.explain(str(args.get("route") or "/")), step
            if name == "find_actions":
                return actions.search(self.index, str(args.get("query") or "")), step
            if name == "describe_action":
                entry = self.index.get(str(args.get("name") or ""))
                if entry is None:
                    step["status"] = "error"
                    return {"error": "unknown action; use find_actions"}, step
                return {**entry, **actions.request_schema(self.app, entry["method"], entry["path"])}, step
            if name == "navigate":
                return {"navigated": args.get("to")}, step
            if name == "call_action":
                action = str(args.get("name") or "")
                step["action"] = action
                entry = self.index.get(action)
                if entry:
                    step["label"] = f"{entry['method']} {entry['path']}"
                arguments = {
                    "path": {k: str(v) for k, v in (args.get("path") or {}).items()},
                    "query": args.get("query") or {},
                    "body": args.get("body"),
                }
                result = await self.gate.dispatch(
                    action,
                    arguments,
                    context=_agent_context(context),
                    origin=f"agent:{thread.id}:{call_id}",
                    thread_id=thread.id,
                    call_id=call_id,
                )
                step["status"] = result.status
                if result.proposal is not None:
                    step["proposal"] = result.proposal.to_dict()
                if result.status == "executed" and isinstance(result.result, dict):
                    step["httpStatus"] = result.result.get("status")
                return result.to_dict(), step
            step["status"] = "error"
            return {"error": f"no tool named {name}"}, step
        except UnknownCapability:
            step["status"] = "error"
            return {"error": "unknown action; use find_actions to get a valid name"}, step
        except ApprovalError as exc:
            step["status"] = "refused"
            return {"error": str(exc), **getattr(exc, "extra", {})}, step
        except Exception as exc:  # the model must see the failure, not a crash
            log.warning("agent.tool_failed", tool=name, exc_info=True)
            step["status"] = "error"
            return {"error": f"{type(exc).__name__}: {exc}"}, step

    async def _complete(
        self, thread: Thread, context: dict[str, Any], *, tools: bool = True
    ) -> AsyncIterator[tuple[str, Any]]:
        """Stream one completion: yields ("text"|"thinking", delta), then ("message", msg)."""
        info = self.state.llm.model_for(MODEL)
        key_id = await self.state.llm.keys.choose(info)
        secret = await self.state.llm.keys.secret(key_id)
        spec = provider_spec(info.provider)
        payload = {
            "model": MODEL,
            "messages": [{"role": "system", "content": _system(context)}, *thread.messages],
            "temperature": 0.2,
            "max_tokens": 4_096,
            "stream": True,
        }
        if tools:
            payload["tools"] = TOOLS
        content: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        tokens = 0
        async with (
            httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0)) as client,
            client.stream(
                "POST",
                f"{spec.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {secret}"},
                json=payload,
            ) as response,
        ):
            if response.status_code >= 400:
                body = (await response.aread()).decode(errors="replace")
                raise RuntimeError(f"LLM {response.status_code}: {body[:300]}")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("usage"):
                    tokens = int(chunk["usage"].get("total_tokens") or tokens)
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        yield "thinking", delta["reasoning_content"]
                    if delta.get("content"):
                        content.append(delta["content"])
                        yield "text", delta["content"]
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(
                            int(tc.get("index", 0)),
                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]
        try:
            await self.state.llm.ledger.record(key_id, MODEL, tokens)
        except Exception:
            log.warning("agent.ledger_failed", exc_info=True)
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        yield "message", message


def _http_status(result: Any) -> int | None:
    return result.get("status") if isinstance(result, dict) else None


def _preview(output: Any) -> str:
    """A short, human-readable glimpse of a tool result for the live trace."""
    if isinstance(output, dict) and "result" in output and output.get("status") == "executed":
        output = (output.get("result") or {}).get("data", output)
    text = json.dumps(output, default=str, ensure_ascii=False)
    return text if len(text) <= 700 else text[:700] + "…"


def _agent_context(context: dict[str, Any]) -> AgentContext:
    pathname = str(context.get("pathname") or "")
    try:
        return AgentContext(pathname=pathname, area=context.get("area"), area_label=context.get("title"))
    except Exception:
        return AgentContext()
