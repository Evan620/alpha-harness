"""How many BRAIN simulations an action will actually launch, asked before it runs.

The gate's generic count reads a ``simulations`` argument, which every derived capability
wraps inside ``body``, so it saw 1 for everything and a goal's ceiling never tripped. The
real cost lives with the work: a lab task spends when it RUNS, not when it is added, and
its planned size is its ``target`` less what it has already ``simulated``. So this asks the
harness the same way the UI would, and the gate refuses on that number.

Anything it cannot price falls back to what the request itself declares; the goal also
re-reads the harness's own launch counter afterwards (:func:`spent_since`), so an estimate
that is wrong low is caught on the next step rather than trusted.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from fastapi import FastAPI

    from .registry import Capability

log = structlog.get_logger(__name__)

RUNNABLE = {"IDLE", "PAUSED", "FAILED"}
COUNT_KEYS = ("simulations", "max_simulations", "maxSimulations", "budget", "n", "count", "target")


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers={"x-harness-client": "1"},
        timeout=30.0,
    )


def _remaining(task: dict[str, Any]) -> int:
    target = int(task.get("target") or 0)
    done = int(task.get("simulated") or 0)
    return max(0, target - done)


def _declared(body: Any) -> int | None:
    """A count the request states itself, at any depth of its body."""
    if isinstance(body, dict):
        for key in COUNT_KEYS:
            value = body.get(key)
            if isinstance(value, list):
                return len(value)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        for value in body.values():
            found = _declared(value)
            if found:
                return found
    if isinstance(body, list) and body and all(isinstance(x, dict) for x in body):
        return len(body)
    return None


async def estimate(app: FastAPI, cap: Capability, arguments: dict[str, Any]) -> int | None:
    """Simulations this call will launch, or ``None`` if it launches none (reads, queue a
    task without running it)."""
    path = cap.path or ""
    body = arguments.get("body")
    params = arguments.get("path") or {}
    try:
        async with _client(app) as client:
            if re.fullmatch(r"/api/lab-tasks/\{task_id\}/run", path):
                tasks = (await client.get("/api/lab-tasks")).json().get("tasks") or []
                wanted = str(params.get("task_id"))
                task = next((t for t in tasks if str(t.get("id")) == wanted), None)
                return _remaining(task) if task else None
            if path == "/api/lab-tasks/run-all":
                tasks = (await client.get("/api/lab-tasks")).json().get("tasks") or []
                return sum(_remaining(t) for t in tasks if t.get("status") in RUNNABLE)
            if path.endswith("/tasks"):
                return 0  # adding a task only queues it; it spends when it runs
            declared = _declared(body)
            if declared:
                return declared
            preview = re.sub(r"/(quick|run)$", "/preview", path)
            if preview != path and cap.method == "POST":
                reply = (await client.post(preview, json=body)).json()
                for key in ("simulations", "count", "total"):
                    value = reply.get(key) if isinstance(reply, dict) else None
                    if isinstance(value, int):
                        return value
                    if isinstance(value, list):
                        return len(value)
    except httpx.HTTPError, ValueError, AttributeError:
        log.info("costs.unpriced", path=path, exc_info=True)
    return None


async def spent_since(state: Any, baseline: int) -> int:
    """Simulations the harness has launched since ``baseline`` today, by its own counter.
    A new platform day restarts the counter, so a figure below the baseline means the day
    rolled over and everything today counts."""
    used = int(await state.tracker.used_today())
    return used - baseline if used >= baseline else used
