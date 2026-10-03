"""Research rounds: one hypothesis, one change, one result. Ported from chijiun/worldquant's
researcher workflow and fitted to this harness.

The model owns research judgment (mechanism, proxy, family, the one change to try, what the
result means); code owns the bookkeeping and the rules a model talks itself past:

- A goal works in ROUNDS. Each round picks exactly one mode and records itself here.
- An ``alpha_designer`` round tests exactly one expression that changes exactly one design
  dimension from its parent. A settings change counts as that dimension.
- Two loops. SEARCH proposes new families until one yields a pass-worthy candidate (no
  failing IS check, fitness at or above :data:`PASS_FITNESS`). IMPROVEMENT then stays on
  that family, one dimension at a time.
- VALIDATION FIRST. Once a family has a pass-worthy candidate, measure its self and prod
  correlation before tuning further; a candidate that cannot clear 0.70 is not improved by
  more fitness.
- STOP RULES, computed here: a family stops after :data:`STOP_NO_IMPROVE` variants in a row
  without a fitness gain of :data:`MEANINGFUL_GAIN`, or after the same dominant failure
  :data:`STOP_REPEAT_FAILURE` times in a row. A stopped family is not retried.

The memory is global, not per goal: a family stopped last week is still a dead end today.
It lives in ``vision_research.json`` beside Vision's other settings.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from .goals import Goal

log = structlog.get_logger(__name__)

MODES = (
    "literature_scout",  # a market mechanism, no formula
    "hypothesis_builder",  # a falsifiable family hypothesis, no formula
    "alpha_designer",  # exactly one expression, one changed dimension
    "result_reflector",  # name the dominant bottleneck, choose one next action
    "validator",  # measure self and prod correlation before more tuning
    "template_governor",  # promote only a robust, interpretable family
)
DECISIONS = (
    "continue_family",
    "new_family",
    "validate_best_candidate",
    "stop_family",
    "promote_template",
    "record_failure",
)
NEXT_ACTIONS = ("test_formula", "validate_checks", "stop_family", "promote_template", "new_family")
#: The dominant reason an experiment fell short. Chijiun's list plus BRAIN's own checks.
TAXONOMY = (
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
)

PASS_FITNESS = 1.0
MEANINGFUL_GAIN = 0.05
STOP_NO_IMPROVE = 5
STOP_REPEAT_FAILURE = 3
MAX_ROUNDS_KEPT = 300
CORR_BAR = 0.70


@dataclass
class Experiment:
    round: int
    alpha_id: str = ""
    expression: str = ""
    parent_alpha_id: str = ""
    changed_dimension: str = ""
    sharpe: float | None = None
    fitness: float | None = None
    turnover: float | None = None
    fails: list[str] = field(default_factory=list)
    self_corr: float | None = None
    prod_corr: float | None = None
    bottleneck: str = ""
    at: float = field(default_factory=time.time)

    @property
    def pass_worthy(self) -> bool:
        return not self.fails and self.fitness is not None and self.fitness >= PASS_FITNESS

    @property
    def validated(self) -> bool:
        """Settled: a measured correlation failed the bar, or both were measured."""
        readings = (self.self_corr, self.prod_corr)
        if any(v is not None and v >= CORR_BAR for v in readings):
            return True
        return all(v is not None for v in readings)


@dataclass
class Family:
    name: str
    mechanism: str = ""
    proxy: str = ""
    direction: str = ""
    horizon: str = ""
    expected_failures: str = ""
    #: exploring -> developing (has a pass-worthy candidate) -> validating -> promoted,
    #: or stopped at any point.
    status: str = "exploring"
    stop_reason: str = ""
    experiments: list[Experiment] = field(default_factory=list)
    best_alpha_id: str = ""
    best_fitness: float | None = None
    no_improve_streak: int = 0
    lessons: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def closed(self) -> bool:
        return self.status in {"stopped", "promoted"}

    def repeat_failure(self) -> str | None:
        """The bottleneck behind the last :data:`STOP_REPEAT_FAILURE` experiments, if it is
        the same one every time."""
        tail = [e.bottleneck for e in self.experiments[-STOP_REPEAT_FAILURE:]]
        if (
            len(tail) == STOP_REPEAT_FAILURE
            and len(set(tail)) == 1
            and tail[0]
            and tail[0] != "no_dominant_bottleneck"
        ):
            return tail[0]
        return None

    def stop_due(self) -> str | None:
        if self.closed:
            return None
        if self.no_improve_streak >= STOP_NO_IMPROVE:
            return (
                f"{self.no_improve_streak} variants in a row without a fitness gain of "
                f"{MEANINGFUL_GAIN}"
            )
        failure = self.repeat_failure()
        if failure:
            return f"the same dominant failure ({failure}) {STOP_REPEAT_FAILURE} times in a row"
        return None

    def candidate_to_validate(self) -> Experiment | None:
        """The best pass-worthy experiment still waiting on validation. One measured
        correlation at or over the bar settles it (it failed); otherwise both are needed."""
        ready = [e for e in self.experiments if e.pass_worthy and not e.validated]
        return max(ready, key=lambda e: e.fitness or 0.0) if ready else None


@dataclass
class Round:
    number: int
    mode: str
    family: str
    decision: str
    next_action: str
    reason: str = ""
    alpha_id: str = ""
    changed_dimension: str = ""
    bottleneck: str = ""
    lesson: str = ""
    goal: str = ""
    at: float = field(default_factory=time.time)


class ResearchLedger:
    """The family memory and the round log, with the stop and validation rules."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.families: dict[str, Family] = {}
        self.rounds: list[Round] = []
        try:
            data = json.loads(path.read_text())
            for raw in data.get("families", []):
                exps = [Experiment(**e) for e in raw.pop("experiments", [])]
                fam = Family(**raw)
                fam.experiments = exps
                self.families[fam.name] = fam
            self.rounds = [Round(**r) for r in data.get("rounds", [])]
        except OSError, ValueError, TypeError, KeyError:
            pass

    # -- recording ---------------------------------------------------------

    def record(self, body: dict[str, Any], *, goal: str = "") -> dict[str, Any]:
        """Record one round. Returns what changed, the warnings, and what the rules now
        require next. Raises ``ValueError`` on a malformed round."""
        mode = str(body.get("mode") or "")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        name = str(body.get("family") or "").strip()
        if not name:
            raise ValueError("family is required: name the family this round belongs to")
        decision = str(body.get("decision") or "continue_family")
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
        next_raw = body.get("next_action") or {}
        next_type = str(next_raw.get("type") if isinstance(next_raw, dict) else next_raw or "")
        if next_type and next_type not in NEXT_ACTIONS:
            raise ValueError(f"next_action.type must be one of {', '.join(NEXT_ACTIONS)}")
        bottleneck = str(body.get("dominant_bottleneck") or "")
        if bottleneck and bottleneck not in TAXONOMY:
            raise ValueError(f"dominant_bottleneck must be one of {', '.join(TAXONOMY)}")

        warnings: list[str] = []
        family = self.families.get(name)
        if family is None:
            family = Family(name=name)
            self.families[name] = family
        hypothesis = body.get("hypothesis") or {}
        if isinstance(hypothesis, dict):
            for key in ("mechanism", "proxy", "direction", "horizon", "expected_failures"):
                if hypothesis.get(key):
                    setattr(family, key, str(hypothesis[key])[:400])
        if family.closed and mode == "alpha_designer":
            warnings.append(
                f"{name} is {family.status} ({family.stop_reason or 'closed'}); it should not be "
                "tested again. Start a new family."
            )
        if mode == "alpha_designer" and not family.mechanism:
            warnings.append(
                f"{name} has no recorded mechanism. Record a hypothesis_builder round "
                "(mechanism, proxy, direction, horizon) before designing more alphas."
            )

        dimension = str(body.get("changed_dimension") or "").strip()
        if mode == "alpha_designer" and any(sep in dimension for sep in (" and ", "+", ",", ";")):
            warnings.append(
                f"changed_dimension {dimension!r} looks like several changes. Change exactly "
                "one dimension per experiment, or the result cannot be attributed."
            )

        metrics = body.get("metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}
        alpha_id = str(body.get("alpha_id") or "").strip()
        measured = any(
            metrics.get(k) is not None
            for k in ("sharpe", "fitness", "turnover", "self_corr", "prod_corr")
        )
        experiment: Experiment | None = None
        # Only a tested alpha is an experiment. A hypothesis or reflection round carries no
        # numbers (an empty fails list is not a measurement) and must not move the streak.
        if alpha_id or measured:
            experiment = self._experiment(family, body, metrics, bottleneck, dimension)
            if experiment.self_corr is not None and experiment.self_corr >= CORR_BAR:
                warnings.append(f"{experiment.alpha_id} self correlation {experiment.self_corr}")
            if experiment.prod_corr is not None and experiment.prod_corr >= CORR_BAR:
                warnings.append(f"{experiment.alpha_id} prod correlation {experiment.prod_corr}")

        lesson = str(body.get("lesson") or "").strip()
        if lesson:
            family.lessons.append(lesson[:400])
            del family.lessons[:-12]
        reason = str(next_raw.get("reason") if isinstance(next_raw, dict) else "")[:400]
        if decision == "stop_family":
            family.status = "stopped"
            family.stop_reason = reason or lesson or "stopped by the researcher"
        elif decision == "promote_template":
            family.status = "promoted"

        rnd = Round(
            number=(self.rounds[-1].number + 1) if self.rounds else 1,
            mode=mode,
            family=name,
            decision=decision,
            next_action=next_type,
            reason=reason,
            alpha_id=alpha_id,
            changed_dimension=dimension,
            bottleneck=bottleneck,
            lesson=lesson[:400],
            goal=goal[:200],
        )
        self.rounds.append(rnd)
        del self.rounds[:-MAX_ROUNDS_KEPT]
        self._save()
        log.info("research.round", number=rnd.number, mode=mode, family=name, decision=decision)
        return {
            "round": asdict(rnd),
            "family": self._family_view(family),
            "warnings": warnings,
            "required": self.required(),
        }

    def _experiment(
        self,
        family: Family,
        body: dict[str, Any],
        metrics: dict[str, Any],
        bottleneck: str,
        dimension: str,
    ) -> Experiment:
        def num(key: str) -> float | None:
            value = metrics.get(key)
            try:
                return None if value is None else float(value)
            except TypeError, ValueError:
                return None

        fails = metrics.get("fails")
        alpha_id = str(body.get("alpha_id") or "").strip()
        # Re-recording an alpha (a validator round adding its correlations) updates the
        # experiment instead of counting a second variant against the stop rule.
        existing = next(
            (e for e in family.experiments if alpha_id and e.alpha_id == alpha_id), None
        )
        if existing is not None:
            for key in ("sharpe", "fitness", "turnover", "self_corr", "prod_corr"):
                value = num(key)
                if value is not None:
                    setattr(existing, key, value)
            if isinstance(fails, list):
                existing.fails = [str(f) for f in fails]
            if bottleneck:
                existing.bottleneck = bottleneck
            self._refresh_status(family)
            return existing

        experiment = Experiment(
            round=(self.rounds[-1].number + 1) if self.rounds else 1,
            alpha_id=alpha_id,
            expression=str(body.get("expression") or "")[:2_000],
            parent_alpha_id=str(body.get("parent_alpha_id") or ""),
            changed_dimension=dimension[:200],
            sharpe=num("sharpe"),
            fitness=num("fitness"),
            turnover=num("turnover"),
            fails=[str(f) for f in fails] if isinstance(fails, list) else [],
            self_corr=num("self_corr"),
            prod_corr=num("prod_corr"),
            bottleneck=bottleneck,
        )
        family.experiments.append(experiment)
        best = family.best_fitness
        if experiment.fitness is not None and (
            best is None or experiment.fitness >= best + MEANINGFUL_GAIN
        ):
            family.best_fitness = experiment.fitness
            family.best_alpha_id = experiment.alpha_id
            family.no_improve_streak = 0
        else:
            family.no_improve_streak += 1
            if experiment.fitness is not None and (best is None or experiment.fitness > best):
                family.best_fitness, family.best_alpha_id = experiment.fitness, experiment.alpha_id
        self._refresh_status(family)
        return experiment

    @staticmethod
    def _refresh_status(family: Family) -> None:
        if family.closed:
            return
        if family.candidate_to_validate() is not None:
            family.status = "validating"
        elif any(e.pass_worthy for e in family.experiments):
            family.status = "developing"

    # -- reading -----------------------------------------------------------

    def required(self) -> list[str]:
        """What the rules require before anything else, most urgent first."""
        out: list[str] = []
        for fam in self.families.values():
            due = fam.stop_due()
            if due:
                out.append(
                    f"STOP family {fam.name!r}: {due}. Record a stop_family round with the lesson, "
                    "then start a new family."
                )
        for fam in self.families.values():
            cand = None if fam.closed else fam.candidate_to_validate()
            if cand is not None:
                out.append(
                    f"VALIDATE {cand.alpha_id} in {fam.name!r} (fitness {cand.fitness}, no failing "
                    "check) before more tuning: read its self and prod correlation and record a "
                    "validator round."
                )
        return out

    def focus(self) -> str:
        """``improvement`` while an open family has a pass-worthy candidate, else ``search``."""
        open_ready = [f for f in self.families.values() if not f.closed and f.status != "exploring"]
        return "improvement" if open_ready else "search"

    def rounds_since(self, since: float) -> int:
        return sum(1 for r in self.rounds if r.at >= since)

    def _family_view(self, fam: Family) -> dict[str, Any]:
        last = fam.experiments[-1] if fam.experiments else None
        return {
            "name": fam.name,
            "status": fam.status,
            "stopReason": fam.stop_reason,
            "mechanism": fam.mechanism,
            "proxy": fam.proxy,
            "experiments": len(fam.experiments),
            "bestAlphaId": fam.best_alpha_id,
            "bestFitness": fam.best_fitness,
            "noImproveStreak": fam.no_improve_streak,
            "lastBottleneck": last.bottleneck if last else "",
            "stopDue": fam.stop_due(),
            "lessons": fam.lessons[-4:],
        }

    def state(self) -> dict[str, Any]:
        families = sorted(self.families.values(), key=lambda f: f.created_at)
        return {
            "loop": self.focus(),
            "required": self.required(),
            "families": [self._family_view(f) for f in families if not f.closed],
            "closedFamilies": [
                {"name": f.name, "status": f.status, "reason": f.stop_reason}
                for f in families
                if f.closed
            ],
            "recentRounds": [asdict(r) for r in self.rounds[-8:]],
            "rules": {
                "passFitness": PASS_FITNESS,
                "meaningfulGain": MEANINGFUL_GAIN,
                "stopNoImprove": STOP_NO_IMPROVE,
                "stopRepeatFailure": STOP_REPEAT_FAILURE,
                "corrBar": CORR_BAR,
            },
            "modes": list(MODES),
            "decisions": list(DECISIONS),
            "nextActions": list(NEXT_ACTIONS),
            "taxonomy": list(TAXONOMY),
        }

    def summary(self, limit: int = 6) -> str:
        """A compact view for a prompt: the loop, what the rules require, the open families
        and the closed ones not to retry."""
        lines = [f"Research loop: {self.focus().upper()}."]
        required = self.required()
        if required:
            lines.append("Required first:")
            lines.extend(f"- {r}" for r in required[:4])
        open_fams = [f for f in self.families.values() if not f.closed][-limit:]
        if open_fams:
            lines.append("Open families:")
            lines.extend(
                f"- {f.name} [{f.status}] {len(f.experiments)} experiments, best "
                f"{f.best_alpha_id or '-'} fitness {f.best_fitness}, "
                f"{f.no_improve_streak} without gain"
                + (f", last bottleneck {f.experiments[-1].bottleneck}" if f.experiments else "")
                for f in open_fams
            )
        closed = [f.name for f in self.families.values() if f.closed]
        if closed:
            lines.append("Closed, do not retry: " + ", ".join(closed[-12:]))
        if not self.families:
            lines.append("No families yet: start with a hypothesis_builder round.")
        return "\n".join(lines)

    def _save(self) -> None:
        data = {
            "families": [asdict(f) for f in self.families.values()],
            "rounds": [asdict(r) for r in self.rounds],
        }
        try:
            self._path.write_text(json.dumps(data, indent=1))
        except OSError:
            log.warning("research.save_failed", path=str(self._path), exc_info=True)


def ledger_for(app: FastAPI) -> ResearchLedger:
    """The one ledger for this app, created on first use beside Vision's other settings."""
    ledger = getattr(app.state, "research", None)
    if ledger is None:
        ledger = ResearchLedger(app.state.harness.settings.data_dir / "vision_research.json")
        app.state.research = ledger
    return ledger


def round_protocol(goal: Goal, ledger: ResearchLedger, *, unrecorded: bool = False) -> str:
    """The research-round instructions for one goal turn, with the live memory under them."""

    def amount(n: int, noun: str) -> str:
        return f"any number of {noun}s" if n <= 0 else f"at most {n} {noun}{'' if n == 1 else 's'}"

    spend = (
        f"{amount(goal.sims_per_round, 'simulation')} and "
        f"{amount(goal.corr_jobs_per_round, 'correlation job')}"
    )
    lines = [
        "THIS GOAL RUNS AS RESEARCH ROUNDS: one hypothesis, one change, one result.",
        "1. Start the round with GET /api/research/state and do whatever it lists as required"
        " first.",
        "2. Pick exactly ONE mode for this round:",
        "   - hypothesis_builder: a new family's mechanism, observable proxy, direction, horizon"
        " and most likely failure. No formula.",
        "   - alpha_designer: exactly one expression that changes exactly one dimension from its"
        " parent. A settings change counts as that dimension.",
        "   - result_reflector: compare the latest result with its parent and the family best,"
        " name the dominant bottleneck from the taxonomy, choose one next action.",
        "   - validator: measure self and prod correlation for a pass-worthy candidate before"
        " any more tuning.",
        "   - template_governor: promote a family only when it is robust and interpretable.",
        f"3. This round may spend {spend}; the gate refuses more.",
        "4. End the round with POST /api/research/rounds: mode, family, the alpha and expression"
        " tested, its parent, changed_dimension, the measured metrics, the dominant bottleneck,"
        " the decision, ONE next_action, and a lesson when you learned one.",
        f"SEARCH until a family gives a pass-worthy candidate (no failing check, fitness >="
        f" {PASS_FITNESS}), then IMPROVE that family one dimension at a time. A family stops"
        f" after {STOP_NO_IMPROVE} variants without a fitness gain of {MEANINGFUL_GAIN} or the"
        f" same dominant failure {STOP_REPEAT_FAILURE} times in a row; never retry a closed"
        f" family. Hand the person only candidates whose measured self AND prod correlations"
        f" are both below {CORR_BAR}.",
    ]
    if unrecorded:
        lines.insert(
            0,
            "You did not record the last round. First POST /api/research/rounds for it, then"
            " continue.",
        )
    return "\n".join([*lines, "", ledger.summary()])
