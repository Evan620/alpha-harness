"""A standing objective the agent keeps working toward, turn after turn, until it is met.

Modelled on Claude Code's ``/goal`` and Hermes' goal loop. After every turn a separate judge
reads the objective and what the agent showed, and returns one of: met, continue, wait,
blocked, impossible. On continue the agent is prompted to take the next step by itself; on
wait the loop parks (simulations and lab tasks run for minutes, and re-prompting while BRAIN
is still computing is busy-work); the rest end the loop and say why. Hard stops the judge
cannot talk past: the turn cap, the budget, and stagnation (several turns with no action).


Without one, every turn is a fresh instruction and Vision stops the moment it finishes
speaking. A goal is what lets it keep going: an objective, a ceiling on what it may spend
reaching it, and a stopping rule.

**The ceiling is enforced at the gate, not in the prompt.** A budget the model is merely
told about is a suggestion, and the failure mode is a loop that spends the day's whole
simulation allowance before anyone looks up. :class:`GoalBook` is handed to
:class:`ApprovalGate`, which asks it before every execution and tells it what was spent
after. A goal is therefore also the safe way to grant auto mode: "act freely until 1,200
simulations are used on this objective" is a much smaller thing to hand over than "act
freely".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: What a goal meters. Each is counted from the capability's own declared effects, so a new
#: capability is metered by what it declares rather than by a list kept somewhere else.
METERED = ("brain_simulations", "correlation_jobs", "llm_requests")

UNLIMITED = 0
DEFAULT_MAX_TURNS = 20
#: Turns with no action before the loop decides it is talking rather than working.
STALL_TURNS = 3
#: Judge replies that could not be read before the loop pauses and says so.
MAX_JUDGE_FAILURES = 3
RUNNING = frozenset({"active", "waiting"})


@dataclass
class Goal:
    """One objective and what it may spend. ``0`` on a budget means no ceiling."""

    objective: str
    #: How to tell it is met, in the person's words. The judge holds the agent to it.
    done_when: str = ""
    brain_simulations: int = 0
    llm_requests: int = 0
    correlation_jobs: int = 0
    max_turns: int = DEFAULT_MAX_TURNS
    spent: dict[str, int] = field(default_factory=lambda: dict.fromkeys(METERED, 0))
    created_at: float = field(default_factory=time.time)
    #: active | waiting | paused, while it runs; met | impossible | blocked | spent |
    #: turns_out | stalled | stopped once it has ended.
    status: str = "active"
    stopped_reason: str = ""
    turns: int = 0
    #: Consecutive turns in which the agent called no action. Stagnation, not thinking.
    idle_turns: int = 0
    judge_failures: int = 0
    last_verdict: str = ""
    last_reason: str = ""
    #: Epoch seconds when a WAIT releases.
    wait_until: float = 0.0
    thread_id: int | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.status in RUNNING

    def note(self, verdict: str, reason: str) -> None:
        self.last_verdict, self.last_reason = verdict, reason
        self.history.append(
            {"turn": self.turns, "verdict": verdict, "reason": reason, "at": time.time()}
        )
        del self.history[:-30]

    def end(self, status: str, reason: str) -> None:
        self.status, self.stopped_reason = status, reason
        self.note(status, reason)
        log.info("agent.goal.ended", status=status, reason=reason[:160])

    def budget(self, meter: str) -> int:
        return int(getattr(self, meter, UNLIMITED))

    def remaining(self, meter: str) -> int | None:
        """``None`` when uncapped, so a caller cannot mistake "no ceiling" for "none left"."""
        cap = self.budget(meter)
        if cap <= UNLIMITED:
            return None
        return max(0, cap - self.spent.get(meter, 0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "doneWhen": self.done_when,
            "status": self.status,
            "running": self.running,
            "stoppedReason": self.stopped_reason,
            "createdAt": self.created_at,
            "turns": self.turns,
            "maxTurns": self.max_turns,
            "lastVerdict": self.last_verdict,
            "lastReason": self.last_reason,
            "waitUntil": self.wait_until or None,
            "threadId": self.thread_id,
            "history": self.history[-12:],
            "budgets": {m: self.budget(m) for m in METERED},
            "spent": {m: self.spent.get(m, 0) for m in METERED},
            "remaining": {m: self.remaining(m) for m in METERED},
        }


class GoalBook:
    """The active goal, and the meter the gate consults.

    Kept in memory deliberately: a goal is a session's intent, and one silently resumed by a
    restart would spend against an objective nobody restated.
    """

    def __init__(self) -> None:
        self._goal: Goal | None = None

    @property
    def goal(self) -> Goal | None:
        return self._goal

    def set(self, goal: Goal) -> Goal:
        self._goal = goal
        log.info(
            "agent.goal.set",
            objective=goal.objective[:120],
            simulations=goal.brain_simulations,
        )
        return goal

    def clear(self, reason: str = "cleared") -> None:
        if self._goal is not None:
            self._goal.status = "stopped"
            self._goal.stopped_reason = reason
            log.info("agent.goal.cleared", reason=reason)
        self._goal = None

    # -- the gate's interface --------------------------------------------

    def refusal(self, spends: dict[str, int]) -> str | None:
        """Why this action may not run, or ``None``. Called before every execution.

        With no goal there is no budget, so nothing is refused: a goal adds a ceiling, it
        never becomes the only way to act.
        """
        goal = self._goal
        if goal is None:
            return None
        for meter in METERED:
            left = goal.remaining(meter)
            want = int(spends.get(meter, 0))
            if left is not None and want > left:
                pretty = meter.replace("_", " ")
                return (
                    f"The goal's budget is spent: this needs {want} {pretty} and {left} "
                    f"of {goal.budget(meter)} remain. Raise the budget or set a new goal."
                )
        return None

    def record(self, spends: dict[str, int]) -> None:
        """Count what an execution actually used, and retire the goal when a meter runs out."""
        goal = self._goal
        if goal is None:
            return
        for meter in METERED:
            goal.spent[meter] = goal.spent.get(meter, 0) + int(spends.get(meter, 0))
        for meter in METERED:
            left = goal.remaining(meter)
            if left is not None and left <= 0 and goal.running:
                goal.end("spent", f"the {meter.replace('_', ' ')} budget is used up")
                return
