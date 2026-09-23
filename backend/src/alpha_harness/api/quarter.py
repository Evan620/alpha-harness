"""The quarter BRAIN judges a consultant on.

Levels are evaluated quarterly against three criteria, two of which are countable here
(``docs/learn/consultant-information/brain-genius``): signals submitted in the quarter, and
pyramids formulated in it. The third, combined Alpha performance, is BRAIN's own arithmetic
and is not guessed at.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import structlog
from fastapi import APIRouter

from ..schemas import Out
from .deps import State

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/quarter", tags=["quarter"])

#: "A consultant is considered to have formulated a pyramid if they have submitted a minimum
#: of 3 Alphas in it" — ``docs/learn/consultant-information/brain-genius``.
ALPHAS_PER_PYRAMID = 3


def quarter_bounds(today: date) -> tuple[date, date]:
    """The calendar quarter ``today`` falls in, both ends inclusive."""
    first = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    year, month = (first.year + 1, 1) if first.month == 10 else (first.year, first.month + 3)
    return first, date(year, month, 1) - timedelta(days=1)


class ConsultantStanding(Out):
    """BRAIN's own leaderboard row. Empty below Consultant level, where it is not served."""

    available: bool
    #: 0..1, the out-of-sample performance of the recent-quarter combination. Averages 0.5.
    value_factor: float | None = None
    #: Daily Osmosis Rank: the allocation share the pool earns you day to day.
    daily_osmosis_rank: float | None = None
    weight_factor: float | None = None
    #: Mean correlation of your submitted Alphas against the production pool. Lower is better:
    #: Value Factor rewards Alphas that diversify the combination rather than repeat it.
    mean_prod_correlation: float | None = None
    #: Mean correlation of your submitted Alphas against each other. Lower is better.
    mean_self_correlation: float | None = None
    #: Distinct data fields used across submissions. Breadth, and an Osmosis input.
    data_fields_used: int | None = None
    submissions_count: int | None = None
    super_alpha_submissions_count: int | None = None
    super_alpha_mean_prod_correlation: float | None = None
    super_alpha_mean_self_correlation: float | None = None


class QuarterStanding(Out):
    #: ``2026-Q3``, for a label that needs no explaining.
    label: str
    start: str
    end: str
    #: Alphas submitted inside the quarter, summed from BRAIN's own daily counts.
    submitted: int
    #: Pyramids with at least :data:`ALPHAS_PER_PYRAMID` submitted Alphas in them.
    pyramids_formulated: int
    #: Pyramids with at least one submitted Alpha but not yet enough to count.
    pyramids_started: int
    #: How many Alphas a pyramid needs, so the UI can say it without repeating the rule.
    alphas_per_pyramid: int


@router.get("/consultant")
async def consultant(state: State) -> ConsultantStanding:
    """Value Factor, Daily Osmosis Rank and the mean correlations they turn on.

    The levers, stated once so they are not re-derived: Value Factor rises when submitted
    Alphas *diversify* the production pool, so mean production correlation is the number to
    push down; Osmosis needs breadth, ten Alphas in each of three scopes.
    """
    row = (await state.endpoints.consultant_standing()).get("leaderboard") or {}
    if not row:
        return ConsultantStanding(available=False)
    return ConsultantStanding(
        available=True,
        value_factor=row.get("valueFactor"),
        daily_osmosis_rank=row.get("dailyOsmosisRank"),
        weight_factor=row.get("weightFactor"),
        mean_prod_correlation=row.get("meanProdCorrelation"),
        mean_self_correlation=row.get("meanSelfCorrelation"),
        data_fields_used=row.get("dataFieldsUsed"),
        submissions_count=row.get("submissionsCount"),
        super_alpha_submissions_count=row.get("superAlphaSubmissionsCount"),
        super_alpha_mean_prod_correlation=row.get("superAlphaMeanProdCorrelation"),
        super_alpha_mean_self_correlation=row.get("superAlphaMeanSelfCorrelation"),
    )


@router.get("")
async def standing(state: State) -> QuarterStanding:
    """Submitted Alphas and formulated pyramids for the quarter in progress.

    Two reads, run together. Neither is derivable from the other: an Alpha belongs to as
    many pyramids as it has data categories, so the pyramid counts sum to more than the
    number of Alphas and cannot stand in for it.
    """
    start, end = quarter_bounds(datetime.now(UTC).date())
    days, pyramids = await asyncio.gather(
        state.endpoints.submission_activity(),
        state.endpoints.pyramid_alphas(start.isoformat(), end.isoformat()),
        return_exceptions=True,
    )
    if isinstance(days, BaseException):
        log.warning("quarter.submissions_failed", exc_info=days)
        days = []
    if isinstance(pyramids, BaseException):
        log.warning("quarter.pyramids_failed", exc_info=pyramids)
        pyramids = []

    first, last = start.isoformat(), end.isoformat()
    submitted = sum(count for day, count in days if first <= day <= last)
    counts = [int(p.get("alphaCount") or 0) for p in pyramids]
    return QuarterStanding(
        label=f"{start.year}-Q{(start.month - 1) // 3 + 1}",
        start=first,
        end=last,
        submitted=submitted,
        pyramids_formulated=sum(1 for n in counts if n >= ALPHAS_PER_PYRAMID),
        pyramids_started=sum(1 for n in counts if 0 < n < ALPHAS_PER_PYRAMID),
        alphas_per_pyramid=ALPHAS_PER_PYRAMID,
    )
