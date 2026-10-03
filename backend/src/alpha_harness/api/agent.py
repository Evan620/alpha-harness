"""Vision, the in-app agent: streamed turns and decisions (NDJSON), plus the action catalog."""

from __future__ import annotations

import difflib
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..agent.goals import Goal
from ..agent.loop import MODEL, AgentService

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
    #: Research rounds: what one goal turn may spend. 1 simulation is one experiment per
    #: round; 2 correlation jobs are one candidate's self and prod reads. 0 removes the cap.
    sims_per_round: int = Field(default=1, ge=0, le=100)
    corr_jobs_per_round: int = Field(default=2, ge=0, le=50)
    #: The conversation to carry on in, and the page the person is on.
    thread_id: int | None = None
    context: PageContext = Field(default_factory=PageContext)


@router.get("/goal")
async def get_goal(request: Request) -> dict[str, Any]:
    """The current goal, and the last one that finished (a finished goal clears itself)."""
    goals = (await _service(request)).goals
    goal, last = goals.goal, goals.last
    return {"goal": goal.to_dict() if goal else None, "last": last.to_dict() if last else None}


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
            sims_per_round=payload.sims_per_round,
            corr_jobs_per_round=payload.corr_jobs_per_round,
            thread_id=payload.thread_id,
            context=_context(payload.context),
            baseline_used=int(await service.state.tracker.used_today()),
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
async def events(request: Request, since: Annotated[int, Query(ge=-1)] = -1) -> StreamingResponse:
    """Everything Vision does on its own (goal turns, verdicts, waits), as NDJSON, from
    ``since`` onward and then live. A reopened panel catches up from where it left off."""
    service = await _service(request)
    # -1: a panel opening fresh. Replay the current goal's story, not the whole log.
    # A position past the end is from before a backend restart (the log starts again at 1):
    # treat that panel as opening fresh, or it would miss everything until the count caught up.
    start = service.bus.goal_from if since < 0 or since > service.bus.seq else since

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


class RuleDecision(BaseModel):
    decision: Literal["accept", "reject"]


@router.post("/rules/{rule_id}")
async def decide_rule(rule_id: int, payload: RuleDecision, request: Request) -> dict[str, Any]:
    """Accept or reject a rule Vision proposed. The person's alone: this route is outside
    Vision's action catalog, so it cannot accept its own proposal."""
    from datetime import UTC, datetime

    from ..db.models import DoctrineRule

    service = await _service(request)
    async with service.state.db.session() as session:
        row = await session.get(DoctrineRule, rule_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No rule with that number.")
        row.status = "accepted" if payload.decision == "accept" else "rejected"
        row.decided_at = datetime.now(UTC)
        text, status = row.text, row.status
    service.bus.publish(
        {"type": "loop", "kind": "status", "text": f"Rule #{rule_id} {status}: {text}"}
    )
    return {"id": rule_id, "status": status, "text": text}


@router.post("/playbooks/{playbook_id}/archive")
async def archive_playbook(playbook_id: int, request: Request) -> dict[str, Any]:
    from ..db.models import Playbook

    service = await _service(request)
    async with service.state.db.session() as session:
        row = await session.get(Playbook, playbook_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No playbook with that number.")
        row.status = "archived"
        name = row.name
    return {"id": playbook_id, "status": "archived", "name": name}


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


@router.get("/watches")
async def watches(request: Request) -> list[dict[str, Any]]:
    """Work Vision is monitoring, across conversations (/watches)."""
    return (await _service(request)).watcher.active()


@router.delete("/watches/{watch_id}")
async def cancel_watch(watch_id: int, request: Request) -> dict[str, Any]:
    service = await _service(request)
    if not service.watcher.cancel(watch_id):
        raise HTTPException(
            status_code=404, detail={"code": "no_watch", "message": f"No watch #{watch_id}."}
        )
    return {"cancelled": watch_id, "active": service.watcher.active()}


@router.get("/sessions")
async def sessions(request: Request) -> list[dict[str, Any]]:
    """Vision's conversations, newest first, marking the one a goal runs in (/sessions)."""
    return (await _service(request)).sessions()


@router.get("/sessions/{thread_id}")
async def session(thread_id: int, request: Request) -> dict[str, Any]:
    """One conversation rebuilt as panel entries, to resume it (/resume)."""
    entries = (await _service(request)).history(thread_id)
    if entries is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "no_session", "message": f"No session #{thread_id}."},
        )
    return {"id": thread_id, "entries": entries}


class ModelRequest(BaseModel):
    """A roster model id, or a provider plus a slug pasted from its catalog. ``model`` null
    goes back to the default, which falls back to whatever the Keys can reach."""

    model: str | None = Field(default=None, max_length=200)
    provider: str | None = Field(default=None, max_length=40)


async def _model_view(service: AgentService, request: Request) -> dict[str, Any]:
    llm = request.app.state.harness.llm
    keyed = {row.provider for row in await llm.keys.list_keys() if row.enabled}
    effective: dict[str, Any] | None = None
    try:
        info = await llm.resolve(service.model_choice.model or MODEL, deep=True)
        effective = {"id": info.id, "label": info.label, "provider": info.provider}
    except Exception:  # noqa: BLE001 - no keys yet: the view says so instead of failing
        effective = None
    options = [m.to_dict() for m in llm.registry.all("text") if m.provider in keyed]
    from ..llm.providers import get as provider_spec

    providers = []
    for pid in sorted(keyed):
        spec = provider_spec(pid)
        providers.append({"id": pid, "label": spec.label, "paid": spec.paid})
    return {
        "model": service.model_choice.model,
        "default": MODEL,
        "effective": effective,
        "options": options,
        "providers": providers,
    }


@router.get("/model")
async def get_model(request: Request) -> dict[str, Any]:
    service = await _service(request)
    return await _model_view(service, request)


@router.put("/model")
async def set_model(payload: ModelRequest, request: Request) -> dict[str, Any]:
    """Set by the person in the UI. Outside Vision's catalog, so it cannot switch its own model."""
    service = await _service(request)
    llm = request.app.state.harness.llm
    model = (payload.model or "").strip() or None
    if model is None:
        service.model_choice.set(None)
        return await _model_view(service, request)
    known = llm.registry.get(model)
    provider = (payload.provider or (known.provider if known else "")).strip()
    if not provider:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "provider_required",
                "message": "Say which provider serves this model.",
            },
        )
    keyed = {row.provider for row in await llm.keys.list_keys() if row.enabled}
    if provider not in keyed:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "no_key",
                "message": f"Add an enabled {provider} key first; Vision can only use "
                "a model a key answers for.",
            },
        )
    if known is None or known.discovered or known.provider != provider:
        offered = await llm.provider_models(provider)
        if offered is None:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "unverified",
                    "message": f"Could not read {provider}'s model list to check "
                    f"{model!r}. Try again.",
                },
            )
        if model not in offered:
            tail = model.split("/")[-1].lower()
            near = [m for m in offered if tail in m.lower()]
            near += difflib.get_close_matches(model, offered, n=3, cutoff=0.6)
            near = list(dict.fromkeys(near))[:5]
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "not_offered",
                    "message": f"{provider} does not serve {model!r}."
                    + (f" Did you mean: {', '.join(near)}?" if near else ""),
                },
            )
        service.model_choice.add_custom(model, provider)
    service.model_choice.set(model)
    return await _model_view(service, request)


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
