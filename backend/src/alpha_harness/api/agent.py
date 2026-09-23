"""Vision, the in-app agent: streamed turns and decisions (NDJSON), plus the action catalog."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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


def _service(request: Request) -> AgentService:
    service = getattr(request.app.state, "agent", None)
    if service is None:
        service = AgentService(request.app, request.app.state.harness)
        request.app.state.agent = service
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
    service = _service(request)
    return _ndjson(service.turn(payload.thread_id, payload.text, _context(payload.context)))


@router.post("/proposals/{proposal_id}/decide")
async def decide(proposal_id: str, payload: DecisionRequest, request: Request) -> StreamingResponse:
    service = _service(request)
    return _ndjson(
        service.decide(
            proposal_id, payload.payload_hash, payload.approve, _context(payload.context)
        )
    )


class PermissionsRequest(BaseModel):
    mode: Literal["ask", "auto"]


@router.get("/permissions")
async def get_permissions(request: Request) -> dict[str, Any]:
    service = _service(request)
    return {"mode": service.permissions.mode, "alwaysYours": _always_yours(service)}


@router.put("/permissions")
async def set_permissions(payload: PermissionsRequest, request: Request) -> dict[str, Any]:
    """Set by the person in the UI. Excluded from Vision's catalog, so it cannot set its own."""
    service = _service(request)
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
    return [p.to_dict() for p in _service(request).gate.pending()]


@router.get("/actions")
async def catalog(request: Request) -> list[dict[str, Any]]:
    return _service(request).catalog()
