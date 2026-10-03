"""Research rounds for Vision's goal loop: the family memory's rules and the per-round caps.

Ported from chijiun/worldquant's researcher workflow. Each test pins one rule, so removing
the rule fails the test: the stop rules, validation first, the one-dimension warning,
persistence, and the gate's per-round ceiling (which must not touch the person's own turns).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alpha_harness.agent.goals import Goal, GoalBook
from alpha_harness.agent.research import (
    STOP_NO_IMPROVE,
    STOP_REPEAT_FAILURE,
    ResearchLedger,
    round_protocol,
)
from alpha_harness.api import research as research_api

if TYPE_CHECKING:
    from pathlib import Path


def designer(
    family: str, alpha: str, fitness: float, *, bottleneck: str = "fitness_too_low", **kw: Any
) -> dict[str, Any]:
    return {
        "mode": "alpha_designer",
        "family": family,
        "alpha_id": alpha,
        "expression": f"rank(x_{alpha})",
        "changed_dimension": kw.pop("dimension", "window_20_to_60"),
        "metrics": {"fitness": fitness, "sharpe": 1.2, "fails": kw.pop("fails", ["LOW_FITNESS"])},
        "dominant_bottleneck": bottleneck,
        "decision": "continue_family",
        "next_action": {"type": "test_formula", "reason": "try the next window"},
        **kw,
    }


def hypothesis(family: str) -> dict[str, Any]:
    return {
        "mode": "hypothesis_builder",
        "family": family,
        "hypothesis": {"mechanism": "cheap firms re-rate", "proxy": "fcf / ev"},
        "decision": "new_family",
        "next_action": {"type": "test_formula"},
    }


@pytest.fixture
def ledger(tmp_path: Path) -> ResearchLedger:
    return ResearchLedger(tmp_path / "research.json")


def test_stop_rule_after_variants_without_meaningful_gain(ledger: ResearchLedger) -> None:
    ledger.record(hypothesis("value"))
    ledger.record(designer("value", "a0", 0.60))
    # Gains below the meaningful threshold do not reset the streak.
    for i in range(1, STOP_NO_IMPROVE + 1):
        # Alternate the bottleneck so the repeated-failure rule cannot fire first.
        cause = "turnover_too_high" if i % 2 else "returns_too_low"
        ledger.record(designer("value", f"a{i}", 0.60 + 0.01 * i, bottleneck=cause))
    fam = ledger.families["value"]
    assert fam.no_improve_streak == STOP_NO_IMPROVE
    assert fam.stop_due() is not None
    assert any("STOP family 'value'" in r for r in ledger.required())


def test_meaningful_gain_resets_the_streak(ledger: ResearchLedger) -> None:
    ledger.record(designer("value", "a0", 0.60))
    ledger.record(designer("value", "a1", 0.61, bottleneck="turnover_too_high"))
    ledger.record(designer("value", "a2", 0.70, bottleneck="returns_too_low"))
    fam = ledger.families["value"]
    assert fam.no_improve_streak == 0
    assert fam.best_alpha_id == "a2"


def test_stop_rule_after_repeated_dominant_failure(ledger: ResearchLedger) -> None:
    for i in range(STOP_REPEAT_FAILURE):
        ledger.record(designer("mom", f"m{i}", 0.5 + 0.2 * i, bottleneck="sub_universe_fail"))
    fam = ledger.families["mom"]
    assert fam.repeat_failure() == "sub_universe_fail"
    assert "sub_universe_fail" in (fam.stop_due() or "")


def test_no_dominant_bottleneck_never_counts_as_a_repeat(ledger: ResearchLedger) -> None:
    for i in range(STOP_REPEAT_FAILURE):
        ledger.record(designer("mom", f"m{i}", 0.5 + 0.2 * i, bottleneck="no_dominant_bottleneck"))
    assert ledger.families["mom"].repeat_failure() is None


def test_pass_worthy_candidate_demands_validation_first(ledger: ResearchLedger) -> None:
    ledger.record(designer("value", "good1", 1.25, fails=[], bottleneck="no_dominant_bottleneck"))
    fam = ledger.families["value"]
    assert fam.status == "validating"
    assert any("VALIDATE good1" in r for r in ledger.required())
    assert ledger.focus() == "improvement"
    # A validator round adding both correlations updates the same experiment, does not
    # count as a new variant, and clears the demand.
    ledger.record(
        {
            "mode": "validator",
            "family": "value",
            "alpha_id": "good1",
            "metrics": {"self_corr": 0.41, "prod_corr": 0.62},
            "decision": "validate_best_candidate",
            "next_action": {"type": "validate_checks"},
        }
    )
    assert len(fam.experiments) == 1
    assert fam.no_improve_streak == 0
    assert not any("VALIDATE" in r for r in ledger.required())
    assert fam.status == "developing"


def test_high_measured_correlation_is_warned(ledger: ResearchLedger) -> None:
    out = ledger.record(
        designer("value", "hi", 1.3, fails=[])
        | {"metrics": {"fitness": 1.3, "fails": [], "self_corr": 0.2, "prod_corr": 0.74}}
    )
    assert any("prod correlation 0.74" in w for w in out["warnings"])


def test_several_changes_in_one_experiment_are_warned(ledger: ResearchLedger) -> None:
    out = ledger.record(designer("value", "a0", 0.6, dimension="decay 4 and window 60"))
    assert any("several changes" in w for w in out["warnings"])
    single = ledger.record(designer("value", "a1", 0.6, dimension="decay_4_to_6"))
    assert not any("several changes" in w for w in single["warnings"])


def test_stopped_family_is_closed_and_listed(ledger: ResearchLedger) -> None:
    ledger.record(designer("value", "a0", 0.6))
    ledger.record(
        {
            "mode": "result_reflector",
            "family": "value",
            "decision": "stop_family",
            "next_action": {"type": "new_family", "reason": "turnover never fell"},
            "lesson": "fcf/ev with decay 0 churns",
        }
    )
    fam = ledger.families["value"]
    assert fam.status == "stopped"
    assert fam.stop_reason == "turnover never fell"
    assert "Closed, do not retry: value" in ledger.summary()
    again = ledger.record(designer("value", "a9", 0.7))
    assert any("should not be tested again" in w for w in again["warnings"])


def test_designing_without_a_hypothesis_is_warned(ledger: ResearchLedger) -> None:
    out = ledger.record(designer("bare", "b0", 0.5))
    assert any("no recorded mechanism" in w for w in out["warnings"])


def test_malformed_rounds_are_rejected(ledger: ResearchLedger) -> None:
    with pytest.raises(ValueError, match="mode"):
        ledger.record({"mode": "guess", "family": "f", "next_action": {"type": "test_formula"}})
    with pytest.raises(ValueError, match="dominant_bottleneck"):
        ledger.record(designer("f", "a", 0.5, bottleneck="vibes"))


def test_memory_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "research.json"
    first = ResearchLedger(path)
    first.record(hypothesis("value"))
    first.record(designer("value", "a0", 0.8))
    second = ResearchLedger(path)
    assert second.families["value"].mechanism == "cheap firms re-rate"
    assert second.families["value"].experiments[0].alpha_id == "a0"
    assert second.rounds[-1].number == 2


def test_protocol_states_caps_and_flags_an_unrecorded_round(ledger: ResearchLedger) -> None:
    goal = Goal(objective="find one", sims_per_round=1, corr_jobs_per_round=2)
    text = round_protocol(goal, ledger)
    assert "at most 1 simulation and at most 2 correlation jobs" in text
    assert "did not record" not in text
    assert "did not record the last round" in round_protocol(goal, ledger, unrecorded=True)


# -- the gate's per-round ceiling ---------------------------------------------------


def test_round_caps_refuse_a_second_simulation_in_a_goal_round() -> None:
    book = GoalBook()
    book.set(Goal(objective="x", brain_simulations=100, sims_per_round=1, corr_jobs_per_round=2))
    book.begin_round()
    assert book.refusal({"brain_simulations": 1}) is None
    book.record({"brain_simulations": 1})
    refusal = book.refusal({"brain_simulations": 1})
    assert refusal is not None and "research round" in refusal
    # Correlation jobs have their own ceiling.
    book.record({"correlation_jobs": 2})
    assert book.refusal({"correlation_jobs": 1}) is not None


def test_round_caps_reset_each_round_and_skip_the_persons_turns() -> None:
    book = GoalBook()
    book.set(Goal(objective="x", sims_per_round=1))
    book.begin_round()
    book.record({"brain_simulations": 1})
    book.end_round()
    # The person's own turn (no round open) is held only to the goal's total budget.
    assert book.refusal({"brain_simulations": 5}) is None
    book.begin_round()
    assert book.refusal({"brain_simulations": 1}) is None


def test_zero_means_no_round_ceiling() -> None:
    book = GoalBook()
    book.set(Goal(objective="x", sims_per_round=0))
    book.begin_round()
    assert book.refusal({"brain_simulations": 50}) is None


def test_a_restored_goal_never_starts_inside_a_round() -> None:
    goal = Goal(objective="x")
    goal.in_round = True
    assert Goal.from_record(goal.to_record()).in_round is False


# -- the API Vision calls -----------------------------------------------------------


def test_api_records_a_round_and_reports_state(tmp_path: Path) -> None:
    app = FastAPI()
    app.state.research = ResearchLedger(tmp_path / "research.json")
    app.include_router(research_api.router)
    client = TestClient(app, base_url="http://127.0.0.1")
    ok = client.post("/api/research/rounds", json=hypothesis("value"))
    assert ok.status_code == 200, ok.text
    bad = client.post(
        "/api/research/rounds",
        json={"mode": "alpha_designer", "family": "value", "next_action": {"type": "dance"}},
    )
    assert bad.status_code == 422
    state = client.get("/api/research/state").json()
    assert state["loop"] == "search"
    assert state["families"][0]["name"] == "value"
    assert state["families"][0]["mechanism"] == "cheap firms re-rate"


def test_a_round_without_an_alpha_or_numbers_is_not_an_experiment(
    ledger: ResearchLedger,
) -> None:
    # The API sends ``metrics: {"fails": []}`` by default; that is not a measurement.
    ledger.record(hypothesis("value") | {"metrics": {"fails": []}})
    fam = ledger.families["value"]
    assert fam.experiments == []
    assert fam.no_improve_streak == 0


def test_one_failed_correlation_settles_validation(ledger: ResearchLedger) -> None:
    ledger.record(designer("opt", "hi", 2.8, fails=[], bottleneck="no_dominant_bottleneck"))
    assert any("VALIDATE hi" in r for r in ledger.required())
    # Self correlation measured over the bar: a prod read cannot rescue it, so stop asking.
    ledger.record(
        {
            "mode": "validator",
            "family": "opt",
            "alpha_id": "hi",
            "metrics": {"self_corr": 0.79},
            "dominant_bottleneck": "self_corr_fail",
            "decision": "continue_family",
            "next_action": {"type": "test_formula"},
        }
    )
    assert not any("VALIDATE" in r for r in ledger.required())
    assert ledger.families["opt"].status == "developing"


def test_a_finished_or_paused_goal_caps_nothing() -> None:
    book = GoalBook()
    goal = Goal(objective="x", correlation_jobs=6)
    book.set(goal)
    book.record({"correlation_jobs": 5})
    assert book.refusal({"correlation_jobs": 2}) is not None  # running: the cap holds
    goal.end("met", "found it")
    assert book.refusal({"correlation_jobs": 2}) is None
    book.record({"correlation_jobs": 2})
    assert goal.spent["correlation_jobs"] == 5  # nothing billed to an ended goal
    goal.status = "paused"
    assert book.refusal({"correlation_jobs": 2}) is None


def test_a_finished_goal_clears_itself_but_a_paused_one_stays() -> None:
    book = GoalBook()
    goal = Goal(objective="find one")
    book.set(goal)
    goal.status = "paused"
    assert book.goal is goal  # paused can be resumed, so it stays current
    goal.status = "active"
    goal.end("met", "found Vka0KdgM")
    assert book.goal is None
    assert book.last is goal and book.last.stopped_reason == "found Vka0KdgM"
