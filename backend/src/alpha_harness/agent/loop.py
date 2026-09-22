"""The in-app agent: a tool-calling loop over every UI action, aware of the current page.

The model sees five tools. Four only read; ``call_action`` reaches the 158 API operations
and always goes through :meth:`ApprovalGate.dispatch`, so a write becomes a proposal the
person approves in the panel rather than something the model does on its own.
"""

from __future__ import annotations

import itertools
import json
import time
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
MAX_ROUNDS = 10
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


def _system(context: dict[str, Any]) -> str:
    page = guide.page_for(context.get("pathname") or "/")
    here = (
        f"{page.title} ({page.route}): {page.summary}\n{page.detail}"
        if page
        else "An unmapped page."
    )
    visible = (context.get("visibleText") or "")[:VISIBLE_CHARS]
    scope = context.get("scope")
    return f"""You are the agent inside Alpha Harness, a local studio for WorldQuant BRAIN research.
You can do anything the person can do in this UI, through tools, and you help them understand the
platform quickly.

HOW TO WORK
- Answer questions about the page they are on first; they are looking at it.
- To act: find_actions -> describe_action (for anything with a body) -> call_action.
- Reads run at once. Writes, simulations and LLM spends come back needs_approval: say plainly what
  you proposed and that it is waiting for their Approve click. Never say it is done until it ran.
- Human-only (sign-in, keys) and blocked actions: tell them where to do it themselves.
- Never submit alphas to BRAIN; that is not something you can do.
- Use navigate when showing them a page helps. Use real numbers from tool results, never invent.
- Plain, short language. Explain BRAIN terms (Sharpe, fitness, turnover, pyramid, Power Pool)
  in one clause when first used. Do not use em-dashes.

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

    async def turn(self, thread_id: int | None, text: str, context: dict[str, Any]) -> dict[str, Any]:
        thread = self._thread(thread_id)
        thread.messages.append({"role": "user", "content": text})
        return await self._run(thread, context)

    async def decide(
        self, proposal_id: str, payload_hash: str | None, approve: bool, context: dict[str, Any]
    ) -> dict[str, Any]:
        proposal = next((p for p in self.gate.pending() if p.id == proposal_id), None)
        thread = self._thread(proposal.thread_id if proposal else None)
        if approve:
            result = await self.gate.approve(proposal_id, payload_hash or "")
            outcome = json.dumps(result.to_dict(), default=str)[:4_000]
            note = f"[The person APPROVED {result.tool}. It ran. Result: {outcome}]"
        else:
            rejected = await self.gate.reject(proposal_id, reason="Rejected in the agent panel")
            note = f"[The person REJECTED {rejected.tool}. Do not retry it unless asked.]"
        thread.messages.append({"role": "user", "content": note + " Continue, briefly."})
        return await self._run(thread, context)

    def catalog(self) -> list[dict[str, Any]]:
        return list(self.index.values())

    # -- loop ------------------------------------------------------------

    def _thread(self, thread_id: int | None) -> Thread:
        if thread_id is not None and thread_id in self.threads:
            return self.threads[thread_id]
        thread = Thread(id=next(self._ids))
        self.threads[thread.id] = thread
        return thread

    async def _run(self, thread: Thread, context: dict[str, Any]) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        navigate_to: str | None = None
        reply = ""
        for _ in range(MAX_ROUNDS):
            message = await self._complete(thread, context)
            thread.messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                reply = str(message.get("content") or "")
                break
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                output, step = await self._tool(thread, call.get("id", ""), name, args, context)
                steps.append(step)
                if step.get("proposal"):
                    proposals.append(step["proposal"])
                if name == "navigate" and step.get("status") == "ok":
                    navigate_to = str(args.get("to") or "")
                thread.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": json.dumps(output, default=str)[:actions.RESULT_CHARS + 500],
                    }
                )
        else:
            reply = "I stopped after several steps without finishing. Tell me how to continue."
        thread.messages = thread.messages[-HISTORY_LIMIT:]
        while thread.messages and thread.messages[0].get("role") == "tool":
            thread.messages.pop(0)
        thread.updated = time.time()
        return {
            "threadId": thread.id,
            "reply": reply,
            "steps": steps,
            "proposals": proposals,
            "navigate": navigate_to,
        }

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

    async def _complete(self, thread: Thread, context: dict[str, Any]) -> dict[str, Any]:
        info = self.state.llm.model_for(MODEL)
        key_id = await self.state.llm.keys.choose(info)
        secret = await self.state.llm.keys.secret(key_id)
        spec = provider_spec(info.provider)
        payload = {
            "model": MODEL,
            "messages": [{"role": "system", "content": _system(context)}, *thread.messages],
            "tools": TOOLS,
            "temperature": 0.2,
            "max_tokens": 4_096,
        }
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{spec.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {secret}"},
                json=payload,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"LLM {response.status_code}: {response.text[:300]}")
        body = response.json()
        usage = body.get("usage") or {}
        try:
            await self.state.llm.ledger.record(key_id, MODEL, int(usage.get("total_tokens") or 0))
        except Exception:
            log.warning("agent.ledger_failed", exc_info=True)
        message = ((body.get("choices") or [{}])[0]).get("message") or {}
        out: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
        if message.get("tool_calls"):
            out["tool_calls"] = message["tool_calls"]
        return out


def _agent_context(context: dict[str, Any]) -> AgentContext:
    pathname = str(context.get("pathname") or "")
    try:
        return AgentContext(pathname=pathname, area=context.get("area"), area_label=context.get("title"))
    except Exception:
        return AgentContext()
