"""Yield Rate: how much of the allowance each lab turns into submittable alphas.

The objective function::

    Y = submittable alphas / simulated alphas,   target Y >= 0.1%

At the full daily allowance that is five submittable alphas a day. Yield rather than
Sharpe, because compute is the resource actually being spent: ranking labs by the quality
of their best result would fund the expensive one forever.

Only *finished* simulations count in the denominator. Counting queued work would make
every lab look worse the moment it was funded.
"""

from __future__ import annotations

import asyncio
import json
from itertools import accumulate
from typing import TYPE_CHECKING, Any, Literal

import structlog
from sqlalchemy import select

from ..brain.schemas import QUICK_MODE
from ..db.models import SimulationRecord

if TYPE_CHECKING:
    from datetime import date

    from ..db.duck import Catalog
    from ..db.sqlite import Database

log = structlog.get_logger(__name__)

#: Checks that label an alpha rather than gate it. The platform reports these as
#: ``WARNING`` on every alpha checked, passing or failing, so judging them would make
#: nothing submittable.
#:
#: ``PROD_CORRELATION`` and ``REGULAR_SUBMISSION`` gate nothing a consultant is kept from
#: submitting, so they are excused too. ``SELF_CORRELATION`` is deliberately *not* here: an
#: alpha too close to the pool genuinely cannot be submitted, and excusing it would hide the
#: signal that should reallocate cores.
IGNORED_CHECKS = frozenset(
    {
        "PROD_CORRELATION",
        "REGULAR_SUBMISSION",
        "MATCHES_COMPETITION",
        "MATCHES_PYRAMID",
        "MATCHES_THEMES",
        "CLUSTER_TEST",
        "OSMOSIS_ALLOCATION",
        "POWER_POOL_DESCRIPTION_LENGTH",
        "POWER_POOL_DESCRIPTION_FORMAT",
    }
)

#: Results that refuse an alpha. ``WARNING`` is not one: a threshold missed by an alpha that
#: qualifies another way (Power Pool, ATOM) stays ``WARNING`` once BRAIN has finished, and BRAIN
#: accepts it. A plain alpha's miss reads ``WARNING`` only while other checks are still
#: ``PENDING``, and becomes ``FAIL`` when they resolve, so the pending state covers it.
REFUSING = frozenset({"FAIL", "ERROR"})

#: What BRAIN's checks say so far: submittable, still being judged, or refused.
Verdict = Literal["submittable", "pending", "refused"]


def checks_of(checks_json: str | None) -> list[dict[str, Any]]:
    """The stored checks array, or empty when missing or unreadable."""
    if not checks_json:
        return []
    try:
        checks = json.loads(checks_json)
    except json.JSONDecodeError, TypeError:
        return []
    return [c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []


def verdict(checks: list[dict[str, Any]], simulation_mode: str | None = None) -> Verdict | None:
    """The one rule for whether an alpha can be submitted, from BRAIN's checks.

    ``None`` when nothing gating was reported (no checks, or only labels): not shown to be good,
    which callers must never read as fine. Otherwise any refusal decides it, then anything
    still ``PENDING``; an alpha whose every gating check is ``PASS`` or ``WARNING`` is
    submittable.

    ``simulation_mode`` is needed because the checks alone cannot answer for a quick-mode
    alpha: BRAIN sends it the performance checks and *omits* every submission check, so one
    that beats every threshold would otherwise read as submittable when the platform will not
    even check it, let alone take it.
    """
    if simulation_mode == QUICK_MODE:
        return "refused"
    results = {
        str(c.get("result", "")).upper()
        for c in checks
        if str(c.get("name", "")).upper() not in IGNORED_CHECKS
    }
    if not results:
        return None
    if results & REFUSING:
        return "refused"
    if results <= {"PASS", "WARNING"}:
        return "submittable"
    return "pending"


def is_submittable(checks_json: str | None, simulation_mode: str | None = None) -> bool:
    """Whether BRAIN has finished and nothing gating refused this alpha."""
    return verdict(checks_of(checks_json), simulation_mode) == "submittable"


def is_promising(checks_json: str | None, simulation_mode: str | None = None) -> bool:
    """Whether this alpha can still come out submittable: nothing refused it, some check PENDING.

    A finished simulation leaves ``SELF_CORRELATION``, ``PROD_CORRELATION``,
    ``REGULAR_SUBMISSION`` and ``IS_LADDER_SHARPE`` ``PENDING`` until BRAIN is asked to check it.
    """
    return verdict(checks_of(checks_json), simulation_mode) == "pending"


class YieldBook:
    """What each lab produced, per unit of allowance spent."""

    def __init__(self, db: Database, catalog: Catalog) -> None:
        self.db = db
        self.catalog = catalog

    async def submittable(
        self,
        *,
        region: str | None = None,
        delay: int | None = None,
        universe: str | None = None,
        instrument_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Every alpha that passed all of BRAIN's submission checks, best Sharpe first, and
        a shortlist of the few worth submitting.

        Alphas already submitted are left out, so nobody submits the same idea twice. Best
        Sharpe first is also most overfit first, so the shortlist ignores it: only Alphas
        that held up in the test years, ranked by :func:`stability_order`, each kept
        unless it moves with a better-ranked pick.
        """
        clauses = ["(a.status IS NULL OR a.status = 'UNSUBMITTED')"]
        params: list[Any] = []
        for column, value in (
            ("region", region),
            ("delay", delay),
            ("universe", universe),
            ("instrument_type", instrument_type),
        ):
            if value is not None:
                clauses.append(f"a.{column} = ?")
                params.append(value)

        # Every row is judged, never a Sharpe-ordered head of the table: a scan capped at a
        # few hundred rows hid every submittable alpha ranked below it. Parsed once per row,
        # off the event loop, because at tens of thousands of alphas that takes seconds.
        every = await self.catalog.query(
            f"SELECT a.alpha_id, a.checks, a.simulation_mode FROM alpha a "  # noqa: S608
            f"WHERE {' AND '.join(clauses)} AND a.checks IS NOT NULL",
            params,
        )
        ready_ids, pending, near = await asyncio.to_thread(_tally, every)
        total = len(ready_ids)
        placeholders = ", ".join("?" for _ in ready_ids)
        ready = (
            await self.catalog.query(
                f"SELECT a.* FROM alpha a WHERE a.alpha_id IN ({placeholders}) "  # noqa: S608
                "ORDER BY a.sharpe DESC NULLS LAST",
                ready_ids,
            )
            if ready_ids
            else []
        )

        # Imported here: both modules read this one's check lists at import time.
        from ..labs.ga import independent, series_of

        validated = [r for r in ready if r.get("test_sharpe") is not None]
        held = [r for r in validated if held_out_ok(r)]
        chosen = ready[:limit]
        # One read for both the ranking and the sparklines.
        days = await self._days_for(
            list(dict.fromkeys(str(r["alpha_id"]) for r in [*held, *chosen]))
        )
        order = stability_order(held)
        picks = await asyncio.to_thread(
            independent,
            order,
            {a: series_of(days[a]) for a in order if a in days},
            SHORTLIST,
            SHORTLIST_MAX_CORRELATION,
        )
        by_id = {str(r["alpha_id"]): r for r in ready}
        # Everything the de-correlation walked past before the shortlist filled up.
        walked = order.index(picks[-1]) + 1 if picks else 0

        ids = list(dict.fromkeys([*picks, *(str(r["alpha_id"]) for r in chosen)]))
        labs = await self._labs_for(ids)
        series = {a: _sparkline(list(d.values())) for a, d in days.items()}

        def entry(row: dict[str, Any]) -> dict[str, Any]:
            alpha_id = str(row["alpha_id"])
            return {
                "alphaId": alpha_id,
                "expression": row.get("expression"),
                "lab": labs.get(alpha_id),
                "dateCreated": (
                    row["date_created"].isoformat() if row.get("date_created") else None
                ),
                "settings": {
                    "region": row.get("region"),
                    "universe": row.get("universe"),
                    "delay": row.get("delay"),
                    "neutralization": row.get("neutralization"),
                    "decay": row.get("decay"),
                    "truncation": row.get("truncation"),
                },
                "sharpe": row.get("sharpe"),
                "fitness": row.get("fitness"),
                "turnover": row.get("turnover"),
                "returns": row.get("returns"),
                "drawdown": row.get("drawdown"),
                "margin": row.get("margin"),
                "trainSharpe": row.get("train_sharpe"),
                "testSharpe": row.get("test_sharpe"),
                "checks": json.loads(row["checks"]) if row.get("checks") else [],
                "pnl": series.get(alpha_id, []),
                "brainUrl": f"{PLATFORM_ALPHA_URL}{alpha_id}",
            }

        return {
            #: The few distinct, held-out-validated Alphas worth a consultant's time.
            "shortlist": [entry(by_id[a]) for a in picks],
            "alphas": [entry(row) for row in chosen],
            "total": total,
            #: Still being judged by the platform. Shown so an empty list reads as
            #: "not yet" rather than "never".
            "pending": pending,
            #: Failing one or two fixable checks, and nothing else.
            "nearMisses": near,
            #: Submittable, but their Sharpe collapsed in the held-out test years.
            "heldOutFailed": len(validated) - len(held),
            #: Submittable, but simulated without a test period, so never validated.
            "unvalidated": len(ready) - len(validated),
            #: Passed over for moving with a better-ranked pick.
            "correlatedPruned": walked - len(picks),
            #: Held-out Alphas with no stored daily PnL, for the caller to download.
            "pnlMissing": [a for a in order if a not in days],
        }

    async def _days_for(self, alpha_ids: list[str]) -> dict[str, dict[date, float]]:
        """Each Alpha's stored daily PnL by date, oldest first. Alphas without one are absent."""
        from .store import AlphaVault

        return await AlphaVault(self.catalog).daily_pnl(alpha_ids)

    async def _labs_for(self, alpha_ids: list[str]) -> dict[str, str]:
        """Which lab produced each alpha, read back off the task name."""
        if not alpha_ids:
            return {}
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    select(SimulationRecord.alpha_id, SimulationRecord.task).where(
                        SimulationRecord.alpha_id.in_(alpha_ids)
                    )
                )
            ).all()
        return {str(a): lab_of(str(t)) for a, t in rows if a}


#: Checks a near miss can plausibly be repaired for. Sharpe is not among them: a signal
#: that is not there cannot be rewritten into one.
FIXABLE_CHECKS = frozenset(
    {
        "HIGH_TURNOVER",
        "LOW_TURNOVER",
        "LOW_FITNESS",
        "CONCENTRATED_WEIGHT",
        "LOW_SUB_UNIVERSE_SHARPE",
        "SELF_CORRELATION",
    }
)

#: Where an alpha lives on the platform. The consultant submits it there, never here.
PLATFORM_ALPHA_URL = "https://platform.worldquantbrain.com/alpha/"

#: Points in a sparkline. More than this is invisible at the size it is drawn.
SPARK_POINTS = 120


def _tally(rows: list[dict[str, Any]]) -> tuple[list[str], int, int]:
    """Submittable ids, promising count and near-miss count, reading each row's JSON once.

    Judged by :func:`verdict`. A near miss is one or two fixable failures and nothing else
    wrong.
    """
    ready: list[str] = []
    pending = near = 0
    for row in rows:
        checks = checks_of(row.get("checks"))
        found = verdict(checks, row.get("simulation_mode"))
        if found == "submittable":
            ready.append(str(row["alpha_id"]))
        elif found == "pending":
            pending += 1
        elif found == "refused":
            failed = {
                name
                for c in checks
                if str(c.get("result", "")).upper() in REFUSING
                and (name := str(c.get("name", "")).upper()) not in IGNORED_CHECKS
            }
            if failed <= FIXABLE_CHECKS and len(failed) <= 2:
                near += 1
    return ready, pending, near


def _sparkline(values: list[float]) -> list[float]:
    """Daily PnL as a cumulative curve, thinned by taking every nth point."""
    cumulative = [round(total, 2) for total in accumulate(values)]
    if len(cumulative) <= SPARK_POINTS:
        return cumulative
    step = len(cumulative) / SPARK_POINTS
    thinned = [cumulative[int(i * step)] for i in range(SPARK_POINTS)]
    # Keep the last point whatever the arithmetic does: the end of the curve is the
    # number the reader actually looks at.
    thinned[-1] = cumulative[-1]
    return thinned


#: Alphas a consultant is shown first. More than a handful is a list to read, not a choice.
SHORTLIST = 5
#: Two picks moving together at |correlation| of this or more are one idea shown twice.
SHORTLIST_MAX_CORRELATION = 0.7
#: The test-years Sharpe must keep at least this share of the train-years Sharpe.
HELD_OUT_KEEP = 0.5


def held_out_ok(row: dict[str, Any]) -> bool:
    """Whether an Alpha held up in the test years its search never saw.

    False when there is no test Sharpe: an Alpha that was never tested has not held up.
    """
    test, train = row.get("test_sharpe"), row.get("train_sharpe")
    if test is None or test <= 0:
        return False
    return train is None or test >= HELD_OUT_KEEP * train


def sub_universe_margin(checks_json: str | None) -> float | None:
    """How far ``LOW_SUB_UNIVERSE_SHARPE`` cleared its limit, as value over limit."""
    for check in checks_of(checks_json):
        if str(check.get("name", "")).upper() != "LOW_SUB_UNIVERSE_SHARPE":
            continue
        value, limit = check.get("value"), check.get("limit")
        if isinstance(value, int | float) and isinstance(limit, int | float) and limit > 0:
            return value / limit
    return None


def stability_order(rows: list[dict[str, Any]]) -> list[str]:
    """Alpha ids, most stable first: the mean percentile rank of test-years Sharpe and
    sub-universe margin.

    Ranks rather than a weighted sum, so no measure needs a scale. A missing measure ranks
    last on that measure.
    """
    ids = [str(r["alpha_id"]) for r in rows]
    measures = [
        {str(r["alpha_id"]): r.get("test_sharpe") for r in rows},
        {str(r["alpha_id"]): sub_universe_margin(r.get("checks")) for r in rows},
    ]
    score = dict.fromkeys(ids, 0.0)
    for measure in measures:
        known = sorted((v, a) for a in ids if (v := measure.get(a)) is not None)
        for place, (_, alpha_id) in enumerate(known, start=1):
            score[alpha_id] += place / len(ids)
    return sorted(ids, key=lambda a: -score[a])


def lab_of(task: str) -> str:
    """Which lab a task name belongs to.

    Task names carry their lab and their day (``sweep-2026-09-08-1``), because quotas and
    progress are keyed by task while yield is judged per lab across many days.
    """
    head = task.split("-", 1)[0]
    return head or "manual"
