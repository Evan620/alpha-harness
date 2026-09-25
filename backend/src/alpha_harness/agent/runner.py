"""The goal loop, run by the backend so it does not depend on a browser tab.

Modelled on Hermes' gateway: the loop lives next to the work, not in the UI. It runs a turn,
asks the judge (:mod:`judge`) what next, and either takes the next step, parks, or stops.
Everything it does is published on the :class:`~bus.EventBus`, which the panel follows, so
closing the tab changes nothing and reopening it replays what happened meanwhile.

**Waiting is woken by the work, not a timer.** When the judge says WAIT, the loop parks until
the simulations or lab tasks that were in flight have finished, which the harness announces
on its own event hub, and uses the judge's wait time only as the ceiling. Nothing polls BRAIN.

Invariants:
- One turn at a time. The runner and a person's own message share ``service.turn_lock``.
- A message from the person pre-empts: the runner's turn in progress is cancelled, the
  person's turn runs, and the loop judges again afterwards (a person's message might be what
  completes the goal).
- A cancelled turn never leaves a tool call without its result in the conversation, which
  the model API would reject on the next turn (see ``loop.repair``).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

import structlog

from . import judge

if TYPE_CHECKING:
    from .loop import AgentService

log = structlog.get_logger(__name__)

#: Harness topics that mean work moved. Any of them may end a WAIT.
WAKE_TOPICS = frozenset({"simulations", "tasks", "studies", "sync"})
#: While waiting on work, look again at least this often even if nothing is announced.
RECHECK_SECONDS = 60.0
#: A burst of events (a batch of ten simulations finishing) is read once, not ten times.
DEBOUNCE_SECONDS = 3.0

START_PROMPT = (
    "[Goal set by the person]\nGoal: {objective}\n{done_when}"
    "Start working toward it: take the first concrete step with a tool."
)
RESUME_PROMPT = (
    "[Resuming your goal]\nGoal: {objective}\n{done_when}"
    "Pick up where you left off: take the next concrete step."
)


class GoalRunner:
    def __init__(self, service: AgentService) -> None:
        self.service = service
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    # -- control -----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, prompt: str | None = None, *, judge_first: bool = False) -> None:
        """Run the loop if a goal is running and the loop is not. ``judge_first`` is for
        after a turn that happened outside the loop: decide what next before acting."""
        goal = self.service.goals.goal
        if goal is None or not goal.running or self.running:
            return
        self._task = asyncio.create_task(self._loop(prompt, judge_first=judge_first))

    async def stop(self) -> None:
        """Cancel whatever the loop is doing and wait for it to let go of the turn lock."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def on_hub(self, topic: str, _payload: Any) -> None:
        """Hub listener. Must not block: it only nudges a parked loop to look again."""
        goal = self.service.goals.goal
        if topic in WAKE_TOPICS and goal is not None and goal.status == "waiting":
            self._wake.set()

    def prompt_for(self, kind: str) -> str:
        goal = self.service.goals.goal
        if goal is None:
            return ""
        done = f"Met when: {goal.done_when}\n" if goal.done_when else ""
        template = START_PROMPT if kind == "start" else RESUME_PROMPT
        return template.format(objective=goal.objective, done_when=done)

    # -- the loop ----------------------------------------------------------

    async def _loop(self, prompt: str | None, *, judge_first: bool) -> None:
        service = self.service
        bus = service.bus
        try:
            while True:
                goal = service.goals.goal
                if goal is None or not goal.running:
                    return
                if goal.status == "waiting":
                    await self._wait()
                    goal = service.goals.goal
                    if goal is None or goal.status != "waiting":
                        return  # paused or cleared while parked
                    prompt = goal.resume_prompt or self.prompt_for("resume")
                    bus.publish(
                        {
                            "type": "loop",
                            "kind": "start",
                            "text": "Work finished, checking back in.",
                        }
                    )
                thread = service.thread_for_goal()
                if not judge_first:
                    await service.run_turn(thread, prompt or self.prompt_for("resume"), auto=True)
                judge_first = False
                prompt = None
                step = await judge.step(service, thread.id)
                await service.persist(thread)
                self._announce(step)
                action = step["action"]
                if action == "continue":
                    prompt = step["prompt"]
                elif action != "wait":
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("goal.runner_failed", exc_info=True)
            goal = service.goals.goal
            if goal is not None and goal.running:
                goal.status = "paused"
                goal.note(
                    "paused", f"The loop hit an error and paused: {type(exc).__name__}: {exc}"
                )
            bus.publish({"type": "loop", "kind": "stop", "text": f"Paused on an error: {exc}"})
        finally:
            goal = service.goals.goal
            bus.publish({"type": "goal", "goal": goal.to_dict() if goal else None})
            with contextlib.suppress(Exception):
                await service.persist()

    async def _wait(self) -> None:
        """Park until the work in flight when the wait began has finished, or the judge's
        ceiling passes. With nothing in flight, the ceiling alone decides."""
        service = self.service
        goal = service.goals.goal
        if goal is None:
            return
        watching = await judge.busy(service)
        while goal.status == "waiting":
            left = goal.wait_until - time.time()
            if left <= 0:
                return
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=min(left, RECHECK_SECONDS))
            if not watching:
                continue
            await asyncio.sleep(DEBOUNCE_SECONDS)
            if not await judge.busy(service):
                log.info("goal.wait_released", reason="work finished")
                return

    def _announce(self, step: dict[str, Any]) -> None:
        bus = self.service.bus
        goal = step.get("goal") or {}
        bus.publish({"type": "goal", "goal": goal})
        action = step["action"]
        reason = goal.get("lastReason") or ""
        if action == "continue":
            text = f"Turn {goal.get('turns', 0) + 1} of {goal.get('maxTurns')}. {reason}"
            bus.publish({"type": "loop", "kind": "continue", "text": text})
        elif action == "wait":
            until = (goal.get("waitUntil") or time.time()) * 1000
            bus.publish({"type": "loop", "kind": "wait", "text": reason, "until": until})
        elif action == "await_approval":
            bus.publish(
                {
                    "type": "loop",
                    "kind": "await_approval",
                    "text": "Paused on the approval card above. Approve or reject to carry on.",
                }
            )
        elif action == "stop":
            live = self.service.goals.goal
            if live is not None:
                thread = self.service.thread_for_goal()
                self.service.reflector.after_goal(
                    live, thread, self.service.used_playbooks.get(thread.id, [])
                )
            label = str(goal.get("status", "")).replace("_", " ")
            text = f"{label.capitalize()}: {goal.get('stoppedReason') or reason}"
            bus.publish({"type": "loop", "kind": "stop", "text": text})
