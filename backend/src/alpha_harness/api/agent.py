"""Vision, the in-app agent: streamed turns and decisions (NDJSON), plus the action catalog."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..agent.goals import Goal
from ..agent.loop import AgentService

router = APIRouter(prefix="/api/agent", tags=["agent"])


class PageContext(BaseModel):
    pathname: str = ""
    title: str | None = None
    area: str | None = None
    scope: dict[str, Any] | None = None
    visible_text: str = Field(default="", max_length=20_000)


class TurnRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8_000)
    thread_id: int | None = None
    context: PageContext = Field(default_factory=PageContext)


class DecisionRequest(BaseModel):
    approve: bool
    payload_hash: str | None = None
    context: PageContext = Field(default_factory=PageContext)


async def _service(request: Request) -> AgentService:
    service = getattr(request.app.state, "agent", None)
    if service is None:
        service = AgentService(request.app, request.app.state.harness)
        request.app.state.agent = service
    await service.load()
    return service


def _context(ctx: PageContext) -> dict[str, Any]:
    return {
        "pathname": ctx.pathname,
        "title": ctx.title,
        "area": ctx.area,
        "scope": ctx.scope,
        "visibleText": ctx.visible_text,
    }


def _ndjson(events: AsyncIterator[dict[str, Any]]) -> StreamingResponse:
    async def body() -> AsyncIterator[bytes]:
        async for event in events:
            yield (json.dumps(event, default=str) + "\n").encode()

    return StreamingResponse(
        body(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/turn")
async def turn(payload: TurnRequest, request: Request) -> StreamingResponse:
    service = await _service(request)
    return _ndjson(service.turn(payload.thread_id, payload.text, _context(payload.context)))


@router.post("/proposals/{proposal_id}/decide")
async def decide(proposal_id: str, payload: DecisionRequest, request: Request) -> StreamingResponse:
    service = await _service(request)
    return _ndjson(
        service.decide(
            proposal_id, payload.payload_hash, payload.approve, _context(payload.context)
        )
    )


class GoalRequest(BaseModel):
    """What to pursue, and the most it may cost. 0 on a budget means no ceiling."""

    objective: str = Field(min_length=3, max_length=600)
    done_when: str = Field(default="", max_length=600, description="How to tell it is met")
    max_turns: int = Field(default=20, ge=1, le=100)
    brain_simulations: int = Field(default=0, ge=0, le=5_000)
    llm_requests: int = Field(default=0, ge=0, le=5_000)
    correlation_jobs: int = Field(default=0, ge=0, le=500)
    #: The conversation to carry on in, and the page the person is on.
    thread_id: int | None = None
    context: PageContext = Field(default_factory=PageContext)


@router.get("/goal")
async def get_goal(request: Request) -> dict[str, Any]:
    goal = (await _service(request)).goals.goal
    return {"goal": goal.to_dict() if goal else None}


def _publish(service: AgentService, kind: str, text: str) -> dict[str, Any]:
    goal = service.goals.goal
    view = goal.to_dict() if goal else None
    service.bus.publish({"type": "goal", "goal": view})
    service.bus.publish({"type": "loop", "kind": kind, "text": text})
    return {"goal": view}


@router.put("/goal")
async def set_goal(payload: GoalRequest, request: Request) -> dict[str, Any]:
    """Set the standing objective and start working on it, in the backend: closing the tab
    does not stop it. The person's to set; Vision has no action for it."""
    service = await _service(request)
    await service.runner.stop()
    goal = service.goals.set(
        Goal(
            objective=payload.objective.strip(),
            done_when=payload.done_when.strip(),
            max_turns=payload.max_turns,
            brain_simulations=payload.brain_simulations,
            llm_requests=payload.llm_requests,
            correlation_jobs=payload.correlation_jobs,
            thread_id=payload.thread_id,
            context=_context(payload.context),
        )
    )
    service.thread_for_goal()
    await service.persist()
    service.bus.mark_goal_start()
    result = _publish(service, "start", f"Goal started: {goal.objective}")
    service.runner.start(service.runner.prompt_for("start"))
    return result


@router.post("/goal/pause")
async def pause_goal(request: Request) -> dict[str, Any]:
    service = await _service(request)
    await service.runner.stop()
    goal = service.goals.goal
    if goal is not None and goal.running:
        goal.status = "paused"
        goal.note("paused", "Paused by the person.")
    await service.persist()
    return _publish(
        service, "stop", "Paused. Carry on with /goal resume, or /goal clear to drop it."
    )


@router.post("/goal/resume")
async def resume_goal(request: Request) -> dict[str, Any]:
    """Resume a paused goal, or give an ended one a fresh turn budget."""
    service = await _service(request)
    goal = service.goals.goal
    if goal is None:
        return {"goal": None}
    if not goal.running and goal.status != "met":
        if goal.turns >= goal.max_turns:
            goal.max_turns = goal.turns + 20
        goal.status, goal.stopped_reason, goal.idle_turns, goal.judge_failures = "active", "", 0, 0
        goal.note("resumed", "Resumed by the person.")
    await service.persist()
    result = _publish(service, "start", "Goal resumed.")
    service.runner.start(service.runner.prompt_for("resume"))
    return result


@router.delete("/goal")
async def clear_goal(request: Request) -> dict[str, Any]:
    service = await _service(request)
    await service.runner.stop()
    service.goals.clear("cleared by the person")
    await service.persist()
    return _publish(service, "stop", "Goal cleared.")


@router.get("/events")
async def events(
    request: Request, since: Annotated[int, Query(ge=-1)] = -1
) -> StreamingResponse:
    """Everything Vision does on its own (goal turns, verdicts, waits), as NDJSON, from
    ``since`` onward and then live. A reopened panel catches up from where it left off."""
    service = await _service(request)
    # -1: a panel opening fresh. Replay the current goal's story, not the whole log.
    start = service.bus.goal_from if since < 0 else since

    async def body() -> AsyncIterator[bytes]:
        async for event in service.bus.follow(start):
            if await request.is_disconnected():
                return
            yield (json.dumps(event, default=str) + "\n").encode()

    return StreamingResponse(
        body(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class PermissionsRequest(BaseModel):
    mode: Literal["ask", "auto"]


@router.get("/permissions")
async def get_permissions(request: Request) -> dict[str, Any]:
    service = await _service(request)
    return {"mode": service.permissions.mode, "alwaysYours": _always_yours(service)}


@router.put("/permissions")
async def set_permissions(payload: PermissionsRequest, request: Request) -> dict[str, Any]:
    """Set by the person in the UI. Excluded from Vision's catalog, so it cannot set its own."""
    service = await _service(request)
    service.permissions.set(payload.mode)
    return {"mode": service.permissions.mode, "alwaysYours": _always_yours(service)}


def _always_yours(service: AgentService) -> list[dict[str, str]]:
    rows = [
        {"action": f"{e['method']} {e['path']}", "summary": e["summary"], "tier": e["tier"]}
        for e in service.index.values()
        if e["tier"] in {"human_only", "blocked"}
    ]
    rows.append(
        {
            "action": "Submit an alpha to BRAIN",
            "summary": "Blocked in the BRAIN client",
            "tier": "blocked",
        }
    )
    return rows


@router.get("/proposals")
async def proposals(request: Request) -> list[dict[str, Any]]:
    return [p.to_dict() for p in (await _service(request)).gate.pending()]


@router.get("/actions")
async def catalog(request: Request) -> list[dict[str, Any]]:
    return (await _service(request)).catalog()
