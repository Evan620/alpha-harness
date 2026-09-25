"""What Vision has learned: playbooks, proposed rules, and what has actually worked.

Three kinds of learning, kept apart on purpose because they deserve different trust:

- **Playbooks** are procedures (Hermes calls them skills). Vision writes and refines them
  itself; the person can archive them. Loaded when a similar task starts.
- **Rules** change the doctrine that steers every decision, so Vision may only *propose*
  one. It steers nothing until the person accepts it (``/rules accept N`` in the panel,
  through ``/api/agent/rules``, which Vision's catalog does not contain).
- **What works** is not an opinion at all: counts over every simulation this machine has
  seen, by dataset and by setting. It cannot hallucinate, so it is what a plan should cite.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from ..db.models import DoctrineRule, Playbook
from ..schemas import Out
from .deps import State

log = structlog.get_logger(__name__)

router = APIRouter(tags=["learning"])

LOW_PROD = 0.6


# -- playbooks -------------------------------------------------------------


class PlaybookRequest(BaseModel):
    name: str = Field(
        min_length=3,
        max_length=120,
        description="Short, reusable, e.g. 'EUR near-miss to submittable'",
    )
    when_to_use: str = Field(max_length=1_000, description="The situation this procedure is for")
    steps: str = Field(
        max_length=4_000, description="Numbered steps that worked, with the actions used"
    )
    pitfalls: str = Field(
        default="", max_length=2_000, description="What went wrong, and how to avoid it"
    )


class PlaybookOut(Out):
    id: int
    name: str
    when_to_use: str
    steps: str
    pitfalls: str
    uses: int
    successes: int
    failures: int
    status: str
    author: str
    updated_at: str


def playbook_out(row: Playbook) -> PlaybookOut:
    return PlaybookOut(
        id=row.id,
        name=row.name,
        when_to_use=row.when_to_use,
        steps=row.steps,
        pitfalls=row.pitfalls,
        uses=row.uses,
        successes=row.successes,
        failures=row.failures,
        status=row.status,
        author=row.author,
        updated_at=row.updated_at.isoformat(),
    )


@router.get("/api/playbooks")
async def list_playbooks(
    state: State,
    q: Annotated[str, Query(description="Matches name, when to use, or steps")] = "",
    include_archived: bool = False,
) -> list[PlaybookOut]:
    """Procedures learned from earlier work. Read one before a similar task."""
    stmt = select(Playbook).order_by(Playbook.successes.desc(), Playbook.updated_at.desc())
    if not include_archived:
        stmt = stmt.where(Playbook.status == "active")
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Playbook.name.ilike(like),
                Playbook.when_to_use.ilike(like),
                Playbook.steps.ilike(like),
            )
        )
    async with state.db.session() as session:
        return [playbook_out(r) for r in (await session.execute(stmt)).scalars().all()]


@router.post("/api/playbooks")
async def write_playbook(payload: PlaybookRequest, state: State) -> PlaybookOut:
    """Create a playbook, or refine the one with the same name. Local, spends nothing."""
    return await upsert_playbook(state, payload.model_dump(), author="vision")


async def upsert_playbook(state: Any, data: dict[str, Any], *, author: str) -> PlaybookOut:
    name = str(data.get("name") or "").strip()[:120]
    async with state.db.session() as session:
        row = (
            (await session.execute(select(Playbook).where(Playbook.name == name))).scalars().first()
        )
        if row is None:
            row = Playbook(name=name, author=author)
            session.add(row)
        row.when_to_use = str(data.get("when_to_use") or row.when_to_use or "")[:1_000]
        row.steps = str(data.get("steps") or row.steps or "")[:4_000]
        row.pitfalls = str(data.get("pitfalls") or row.pitfalls or "")[:2_000]
        row.status = "active"
        await session.flush()
        out = playbook_out(row)
    log.info("learning.playbook_written", name=name, author=author)
    return out


# -- rules -----------------------------------------------------------------


class RuleProposal(BaseModel):
    text: str = Field(min_length=10, max_length=600, description="The rule, as one instruction")
    evidence: str = Field(min_length=10, max_length=2_000, description="What in the data shows it")


class RuleOut(Out):
    id: int
    text: str
    evidence: str
    status: str
    author: str
    created_at: str


def rule_out(row: DoctrineRule) -> RuleOut:
    return RuleOut(
        id=row.id,
        text=row.text,
        evidence=row.evidence,
        status=row.status,
        author=row.author,
        created_at=row.created_at.isoformat(),
    )


@router.post("/api/doctrine/proposals")
async def propose_rule(payload: RuleProposal, state: State) -> RuleOut:
    """Propose a doctrine rule from evidence. It steers nothing until the person accepts it."""
    return await add_proposal(state, payload.text, payload.evidence, author="vision")


async def add_proposal(state: Any, text: str, evidence: str, *, author: str) -> RuleOut:
    async with state.db.session() as session:
        row = DoctrineRule(
            text=text.strip()[:600], evidence=evidence.strip()[:2_000], author=author
        )
        session.add(row)
        await session.flush()
        out = rule_out(row)
    log.info("learning.rule_proposed", id=out.id)
    return out


@router.get("/api/doctrine")
async def list_rules(state: State) -> list[RuleOut]:
    """Proposed, accepted and rejected rules. Only accepted ones steer the agent."""
    async with state.db.session() as session:
        rows = (
            (await session.execute(select(DoctrineRule).order_by(DoctrineRule.id.desc())))
            .scalars()
            .all()
        )
        return [rule_out(r) for r in rows]


# -- what works --------------------------------------------------------------


def _checks(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [c for c in raw or [] if isinstance(c, dict)]


def _score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    no_fail = active = 0
    prods: list[float] = []
    sharpes: list[float] = []
    for r in rows:
        checks = _checks(r.get("checks"))
        if checks and not any(c.get("result") == "FAIL" for c in checks):
            no_fail += 1
        if r.get("status") == "ACTIVE":
            active += 1
        if isinstance(r.get("sharpe"), (int, float)):
            sharpes.append(float(r["sharpe"]))
        prods.extend(
            float(c["value"])
            for c in checks
            if c.get("name") == "PROD_CORRELATION" and isinstance(c.get("value"), (int, float))
        )
    return {
        "alphas": n,
        "noFailRate": round(no_fail / n, 3) if n else 0.0,
        "submitted": active,
        "avgSharpe": round(sum(sharpes) / len(sharpes), 2) if sharpes else None,
        "prodCorrMeasured": len(prods),
        "avgProdCorr": round(sum(prods) / len(prods), 3) if prods else None,
        "lowProdShare": round(sum(p < LOW_PROD for p in prods) / len(prods), 3) if prods else None,
    }


@router.get("/api/analyse/what-works")
async def what_works(
    state: State,
    region: Annotated[str, Query(description="e.g. USA, EUR, GLB")] = "USA",
    delay: Annotated[int, Query(ge=0, le=1)] = 1,
    min_alphas: Annotated[int, Query(ge=1, le=500)] = 8,
) -> dict[str, Any]:
    """Measured outcomes over every alpha simulated on this machine for one region and delay:
    per dataset (from the fields in each expression) and per neutralization. ``noFailRate``
    is the share with no failing check, ``lowProdShare`` the share of measured production
    correlations below 0.6. Counts, not opinions: cite these when planning a sweep."""
    rows = await state.catalog.query(
        """
        with toks as (
            select a.alpha_id, a.status, a.sharpe, a.checks, a.neutralization,
                   unnest(regexp_extract_all(a.expression, '[A-Za-z][A-Za-z0-9_]{2,}')) as tok
            from alpha a where upper(a.region) = upper(?) and a.delay = ?
        )
        select distinct t.alpha_id, t.status, t.sharpe, t.checks, t.neutralization, d.dataset_id
        from toks t join data_field d
          on d.field_id = t.tok and upper(d.region) = upper(?) and d.delay = ?
        """,
        [region, delay, region, delay],
    )
    by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_neut: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in rows:
        by_dataset[str(r["dataset_id"])].append(r)
        by_neut[str(r.get("neutralization") or "NONE")][r["alpha_id"]] = r
    datasets = [{"dataset": k, **_score(v)} for k, v in by_dataset.items() if len(v) >= min_alphas]
    datasets.sort(
        key=lambda d: (d["noFailRate"], d["submitted"], d["avgSharpe"] or 0), reverse=True
    )
    neutralizations = [
        {"neutralization": k, **_score(list(v.values()))}
        for k, v in by_neut.items()
        if len(v) >= min_alphas
    ]
    neutralizations.sort(key=lambda d: d["noFailRate"], reverse=True)
    return {
        "region": region.upper(),
        "delay": delay,
        "alphasSeen": len({r["alpha_id"] for r in rows}),
        "datasets": datasets[:25],
        "neutralizations": neutralizations,
        "note": "noFailRate: share with no FAIL check. lowProdShare: measured prod corr < 0.6.",
    }
