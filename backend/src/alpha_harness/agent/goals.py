"""A standing objective with a budget, so the agent can work without being watched.

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


@dataclass
class Goal:
    """One objective and what it may spend. ``0`` on a budget means no ceiling."""

    objective: str
    brain_simulations: int = 0
    llm_requests: int = 0
    correlation_jobs: int = 0
    spent: dict[str, int] = field(default_factory=lambda: dict.fromkeys(METERED, 0))
    created_at: float = field(default_factory=time.time)
    #: active | spent | stopped | done
    status: str = "active"
    stopped_reason: str = ""

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
            "status": self.status,
            "stoppedReason": self.stopped_reason,
            "createdAt": self.created_at,
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
        if goal.status != "active":
            return (
                f"The goal is {goal.status} ({goal.stopped_reason or 'no reason given'}). "
                "Set a new goal to continue."
            )
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
            if left is not None and left <= 0:
                goal.status = "spent"
                goal.stopped_reason = f"the {meter.replace('_', ' ')} budget is used up"
                log.info("agent.goal.spent", meter=meter)
                return
