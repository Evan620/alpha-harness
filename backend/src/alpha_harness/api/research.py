"""Research rounds for Vision's goal loop: the family memory and the round log.

One hypothesis, one change, one result per round (see :mod:`..agent.research`). Vision reads
the state at the start of a round and records the round at its end; both are local and spend
nothing, so they run without an approval card.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..agent.research import ledger_for

router = APIRouter(prefix="/api/research", tags=["research"])

Mode = Literal[
    "literature_scout",
    "hypothesis_builder",
    "alpha_designer",
    "result_reflector",
    "validator",
    "template_governor",
]
Decision = Literal[
    "continue_family",
    "new_family",
    "validate_best_candidate",
    "stop_family",
    "promote_template",
    "record_failure",
]
NextType = Literal[
    "test_formula", "validate_checks", "stop_family", "promote_template", "new_family"
]
Bottleneck = Literal[
    "sharpe_too_low",
    "fitness_too_low",
    "turnover_too_high",
    "returns_too_low",
    "drawdown_too_high",
    "weight_concentration",
    "sub_universe_fail",
    "robust_universe_fail",
    "regional_check_fail",
    "self_corr_fail",
    "prod_corr_fail",
    "signal_amplitude_destroyed",
    "over_smoothing",
    "hard_filter_destroyed_returns",
    "direction_wrong",
    "operator_invalid",
    "field_invalid",
    "platform_timeout",
    "platform_error",
    "validation_pending",
    "no_dominant_bottleneck",
]


class Hypothesis(BaseModel):
    """What a family bets on, before any formula. Falsifiable, so a result can refute it."""

    mechanism: str = Field(default="", description="The market mechanism, in a sentence")
    proxy: str = Field(default="", description="The observable data that stands in for it")
    direction: str = Field(default="", description="Which way the signal should point")
    horizon: str = Field(default="", description="Expected holding horizon")
    expected_failures: str = Field(default="", description="How it is most likely to fail")


class Metrics(BaseModel):
    """Numbers a tool returned for the alpha. Leave out what was not measured."""

    sharpe: float | None = None
    fitness: float | None = None
    turnover: float | None = None
    fails: list[str] = Field(default_factory=list, description="Names of failing IS checks")
    self_corr: float | None = Field(default=None, description="Measured, numeric only")
    prod_corr: float | None = Field(default=None, description="Measured, numeric only")


class NextAction(BaseModel):
    type: NextType
    reason: str = Field(default="", max_length=400)


class RoundIn(BaseModel):
    """One research round. Record it at the end of every goal turn."""

    mode: Mode
    family: str = Field(min_length=1, max_length=120, description="Snake-case family name")
    decision: Decision = "continue_family"
    next_action: NextAction
    hypothesis: Hypothesis | None = None
    alpha_id: str = Field(default="", max_length=40, description="The alpha this round tested")
    expression: str = Field(default="", max_length=2_000)
    parent_alpha_id: str = Field(default="", max_length=40)
    changed_dimension: str = Field(
        default="", max_length=200, description="The ONE thing changed from the parent"
    )
    metrics: Metrics | None = None
    dominant_bottleneck: Bottleneck | None = None
    lesson: str = Field(
        default="", max_length=400, description="Compact and action-oriented, when one was learned"
    )


@router.get("/state")
async def state(request: Request) -> dict[str, Any]:
    """The research memory: which loop to run (search or improvement), what the rules require
    first (stop a family, validate a candidate), open families with their best candidate, and
    closed families not to retry. Read it at the start of every goal round."""
    return ledger_for(request.app).state()


@router.post("/rounds")
async def record_round(body: RoundIn, request: Request) -> dict[str, Any]:
    """Record the round just finished: mode, family, what was tested, the measured numbers,
    the dominant bottleneck and the one next action. Returns warnings and what the rules
    require next."""
    service = getattr(request.app.state, "agent", None)
    goal = service.goals.goal if service is not None else None
    try:
        return ledger_for(request.app).record(
            body.model_dump(exclude_none=True), goal=goal.objective if goal else ""
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "bad_round", "message": str(exc)}
        ) from exc
