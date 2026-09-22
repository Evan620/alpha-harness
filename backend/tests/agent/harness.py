"""Reusable offline harness primitives for the agent acceptance suite."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import func, select

from alpha_harness.db.models import (
    ApiKey,
    DedupEntry,
    KeyUsage,
    MetadataCache,
    PlanTrack,
    QuotaSnapshot,
    SimulationRecord,
    Study,
    SyncRun,
    TaskQuota,
    Template,
    Trial,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from alpha_harness.state import AppState


class OutboundBrainCall(BaseException):
    """An attempted outbound call that ordinary application handlers cannot swallow.

    This deliberately is not an Exception: engine/slots.py:275 and
    api/search_lab.py:47 catch Exception, which would otherwise hide the evidence.
    """

    def __init__(self, method: str, url: str) -> None:
        self.method = method
        self.url = url
        super().__init__(f"outbound BRAIN call blocked: {method} {url}")


class RecordingTransport(httpx.AsyncBaseTransport):
    """Record every request and deny anything without an explicit canned route."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.routes: list[tuple[str, str, httpx.Response]] = []

    def route(self, method: str, path_prefix: str, response: httpx.Response) -> None:
        self.routes.append((method.upper(), path_prefix, response))

    def reset(self) -> None:
        self.calls.clear()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        url = str(request.url)
        self.calls.append((method, url))
        for route_method, path_prefix, response in self.routes:
            if method == route_method and request.url.path.startswith(path_prefix):
                return response
        raise OutboundBrainCall(method, url)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._handle(request)

    def as_httpx(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)


@dataclass(slots=True)
class Harness:
    app: FastAPI
    client: httpx.AsyncClient
    state: AppState
    brain: RecordingTransport


class ScriptedProvider:
    """Return scripted provider results and fail loudly when the script is exhausted."""

    def __init__(self, script: Sequence[Any]) -> None:
        self._script = list(script)
        self.calls: list[tuple[Any, ...]] = []

    async def converse(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((*args, kwargs))
        if not self._script:
            raise AssertionError("scripted provider was called after its script was exhausted")
        result = self._script.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


_SQLITE_MODELS = (
    SimulationRecord,
    DedupEntry,
    TaskQuota,
    QuotaSnapshot,
    KeyUsage,
    Study,
    Trial,
    SyncRun,
    Template,
    ApiKey,
    PlanTrack,
    MetadataCache,
)


async def snapshot(state: AppState, brain: RecordingTransport) -> dict[str, Any]:
    """Capture every local surface on which an agent action could spend or schedule work."""
    sqlite_counts: dict[str, int] = {}
    async with state.db.session() as session:
        for model in _SQLITE_MODELS:
            count = await session.scalar(select(func.count()).select_from(model))
            sqlite_counts[model.__tablename__] = int(count or 0)

        usage = (
            await session.execute(
                select(
                    func.coalesce(func.sum(KeyUsage.requests), 0),
                    func.coalesce(func.sum(KeyUsage.tokens), 0),
                )
            )
        ).one()
        study_rows = (
            await session.execute(select(Study.id, Study.status).order_by(Study.id))
        ).all()
        simulation_rows = (
            await session.execute(
                select(
                    SimulationRecord.id, SimulationRecord.status, SimulationRecord.platform_id
                ).order_by(SimulationRecord.id)
            )
        ).all()
        sync_rows = (
            await session.execute(select(SyncRun.id, SyncRun.status).order_by(SyncRun.id))
        ).all()

    alpha_checks = await state.catalog.query("SELECT alpha_id, checks FROM alpha ORDER BY alpha_id")
    canonical_checks = json.dumps(
        alpha_checks,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()

    return {
        "sqlite_counts": sqlite_counts,
        "key_usage_totals": {"requests": int(usage[0]), "tokens": int(usage[1])},
        "study_statuses": {row.id: row.status for row in study_rows},
        "simulation_statuses": {row.id: (row.status, row.platform_id) for row in simulation_rows},
        "sync_statuses": {row.id: row.status for row in sync_rows},
        "duckdb_counts": {
            "alpha": int(await state.catalog.scalar("SELECT count(*) FROM alpha") or 0),
            "alpha_pnl": int(await state.catalog.scalar("SELECT count(*) FROM alpha_pnl") or 0),
        },
        "alpha_checks_sha256": hashlib.sha256(canonical_checks).hexdigest(),
        "tasks": copy.deepcopy(await state.tasks.summary()),
        "brain_calls": list(brain.calls),
    }


async def assert_nothing_spent(
    harness: Harness,
    before: dict[str, Any],
    *,
    ticks: int = 2,
) -> None:
    """Drive at least two engine rounds, then prove every spend surface stayed fixed."""
    if ticks < 2:
        raise ValueError("spend assertions must cover at least two BatchEngine ticks")
    for _ in range(ticks):
        await harness.state.engine.tick()

    after = await snapshot(harness.state, harness.brain)
    assert after.keys() == before.keys(), (
        f"spend snapshot shape changed: before={sorted(before)}, after={sorted(after)}"
    )
    for key, expected in before.items():
        actual = after[key]
        assert actual == expected, (
            f"spend surface {key!r} moved: before={expected!r}, after={actual!r}"
        )


def source_files(*roots: str | Path) -> list[Path]:
    """Return Python source files beneath roots in deterministic order."""
    found: set[Path] = set()
    for raw_root in roots:
        root = Path(raw_root)
        if root.is_file():
            found.add(root)
        elif root.is_dir():
            found.update(root.rglob("*.py"))
    return sorted(found, key=lambda path: str(path))


def grep(
    pattern: str | re.Pattern[str],
    files: Sequence[Path],
    *,
    exclude_comments: bool = False,
) -> list[tuple[Path, int, str]]:
    """Search source lines without shelling out, optionally ignoring comment-only lines."""
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    matches: list[tuple[Path, int, str]] = []
    for path in files:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            if exclude_comments and stripped.startswith(("#", "//", "/*", "*")):
                continue
            if compiled.search(line):
                matches.append((path, line_number, line.rstrip()))
    return matches
