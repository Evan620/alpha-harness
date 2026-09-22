"""Which competition is running, and where this account stands in it.

Read-only, and deliberately so: enrolling accepts an agreement in the user's name, which is
the same class of act as submitting an Alpha. The app reports; the user signs up on BRAIN.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter

from ..schemas import Out
from .deps import State

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/competitions", tags=["competitions"])


class Standing(Out):
    """This account's own row on the board. Absent until the first Alpha is scored."""

    rank: int | None
    alphas: int | None
    robustness_score: float | None
    university: str | None
    country: str | None


class Competition(Out):
    id: str
    name: str
    description: str | None
    #: ``ACCEPTED`` once enrolled; the open list carries the competition's own status.
    status: str | None
    team_based: bool
    scoring: str | None
    start_date: str | None
    end_date: str | None
    sign_up_end_date: str | None
    #: Whether this account is in it. The whole point of the screen.
    enrolled: bool
    #: Whole days from now to ``end_date``; negative once it has ended.
    days_left: int | None
    #: Days left to sign up, for a competition not yet joined.
    sign_up_days_left: int | None
    standing: Standing | None
    faq: str | None


class Competitions(Out):
    competitions: list[Competition]
    #: True when any of them is region-agnostic by name. Cheap, and honest about being a
    #: guess: BRAIN publishes no field saying which markets a competition is scored over.
    region_agnostic: bool


def _days_from(now: datetime, raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (when - now).days


def _standing(raw: Any) -> Standing | None:
    if not isinstance(raw, dict):
        return None
    return Standing(
        rank=raw.get("rank"),
        alphas=raw.get("alphas"),
        robustness_score=raw.get("robustnessScore"),
        university=raw.get("university"),
        country=raw.get("country"),
    )


def _one(raw: dict[str, Any], *, enrolled: bool, now: datetime) -> Competition:
    return Competition(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or raw.get("id") or "Competition"),
        description=raw.get("description"),
        status=raw.get("status"),
        team_based=bool(raw.get("teamBased")),
        scoring=raw.get("scoring"),
        start_date=raw.get("startDate"),
        end_date=raw.get("endDate"),
        sign_up_end_date=raw.get("signUpEndDate"),
        enrolled=enrolled,
        days_left=_days_from(now, raw.get("endDate")),
        sign_up_days_left=_days_from(now, raw.get("signUpEndDate")),
        standing=_standing(raw.get("leaderboard")),
        faq=raw.get("faq"),
    )


@router.get("")
async def live(state: State) -> Competitions:
    """Every competition still running, with this account's enrolment folded in.

    Two reads, because neither answers alone: the open list says what is running, and only
    ``/users/self/competitions`` carries the enrolment and the account's own board row. A
    competition the user is in wins, so its richer row is the one shown.
    """
    mine: list[dict[str, Any]] = []
    try:
        mine = await state.endpoints.list_competitions(mine=True)
    except Exception:
        log.warning("competitions.mine_failed", exc_info=True)
    joined = {str(row.get("id")) for row in mine}

    open_now: list[dict[str, Any]] = []
    try:
        open_now = await state.endpoints.list_competitions()
    except Exception:
        log.warning("competitions.list_failed", exc_info=True)

    now = datetime.now(UTC)
    rows = [_one(row, enrolled=True, now=now) for row in mine]
    rows.extend(
        _one(row, enrolled=False, now=now) for row in open_now if str(row.get("id")) not in joined
    )
    # Still running first, then the ones the user is in, then soonest to end.
    rows.sort(
        key=lambda c: (
            c.days_left is not None and c.days_left < 0,
            not c.enrolled,
            # Undated sorts last rather than as if it ended today.
            math.inf if c.days_left is None else c.days_left,
        )
    )
    return Competitions(
        competitions=rows,
        region_agnostic=any("all region" in c.name.lower() for c in rows),
    )
