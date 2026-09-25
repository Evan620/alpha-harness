"""The goal loop's judge and its next step.

After each turn the panel asks :func:`step` what happens next. The answer is decided here,
on the server, so the loop's limits cannot be talked past: the turn cap, the budget and
stagnation are checked in code before the judge is consulted, and the judge is a separate
model call that sees the objective and what the agent actually showed, never the agent's
own opinion of how it is doing.

The judge is strict about evidence (modelled on Hermes): "met" needs the deliverable on the
page, alpha ids and check results, not a sentence claiming success. When it cannot be read,
the loop fails OPEN to continue, because a broken judge must not wedge progress; the turn
cap is the backstop, and repeated failures pause the loop and say so.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from . import costs
from .goals import MAX_JUDGE_FAILURES, STALL_TURNS, Goal

if TYPE_CHECKING:
    from .loop import AgentService, Thread

log = structlog.get_logger(__name__)

WAIT_MIN, WAIT_MAX, WAIT_DEFAULT = 30, 1_800, 120
TRANSCRIPT_CHARS = 9_000

JUDGE_SYSTEM = """\
You are a strict judge deciding whether an autonomous research agent has achieved a goal set
by a person using a WorldQuant BRAIN research workstation. You see the goal, how the person
said to tell it is met, the recent transcript, and what work is currently running.
You cannot run anything; judge only what the transcript shows.

Choose exactly one verdict:
- "met": the goal is achieved AND the transcript shows concrete evidence of it: alpha ids,
  check results, numbers returned by tools, a task created with its id. A claim such as
  "done" or "all requirements met" without that evidence is NOT met.
- "wait": not met, and the next useful step is to wait for work already RUNNING
  (simulations in flight, a lab task with status RUNNING, a sync). Give wait_seconds, 30 to
  1800. Choose this ONLY when acting again now would be busy-work. A task that is IDLE
  (added but never started), PAUSED or FAILED is NOT running: waiting on it waits forever,
  so the verdict is continue, and the next step is to start it.
- "blocked": the agent needs the person: an approval, a decision between options, a
  sign-in or key, or information only they have.
- "impossible": cannot be achieved as stated, e.g. it needs something the agent may never
  do (submitting to BRAIN), or the data does not exist.
- "continue": not met, and there is a concrete next step the agent can take now. This is
  the default when in doubt.

Reply with ONE line of JSON and nothing else:
{"verdict": "met|continue|wait|blocked|impossible", "reason": "<one sentence>",
 "wait_seconds": <int, only for wait>}
"""

CONTINUE_PROMPT = """\
[Continuing toward your goal, turn {turn} of {max_turns}]
Goal: {objective}
{done_when}The judge says: {reason}
Take the next concrete step. When the goal is met, show the evidence (alpha ids with links,
check results). If you need the person, say exactly what you need and stop."""

RESUME_PROMPT = """\
[Checking back in on your goal after waiting]
Goal: {objective}
{done_when}You were waiting because: {reason}
Check whether that work has finished, then take the next concrete step."""


def _transcript(thread: Thread) -> str:
    """The recent turns, newest last, cut to fit. Tool output is shortened, not dropped:
    the evidence the judge needs usually lives there."""
    parts: list[str] = []
    for message in thread.messages[-24:]:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "tool":
            parts.append(f"[tool result] {content[:900]}")
        elif role == "assistant":
            calls = message.get("tool_calls") or []
            names = ", ".join(
                f"{(c.get('function') or {}).get('name')}"
                f"({(c.get('function') or {}).get('arguments', '')[:160]})"
                for c in calls
            )
            if content.strip():
                parts.append(f"[agent] {content[:2_500]}")
            if names:
                parts.append(f"[agent called] {names}")
        elif role == "user":
            parts.append(f"[person or loop] {content[:600]}")
    text = "\n".join(parts)
    return text[-TRANSCRIPT_CHARS:]


LIVE_STATUSES = {"running", "queued", "pending", "active", "waiting"}


async def work_in_flight(service: AgentService) -> tuple[int, list[str]]:
    """Simulations in flight, and the names of lab tasks still running, read the way the UI
    reads them. The judge uses this to choose WAIT; the runner uses it to know a WAIT is over."""
    sims, tasks = 0, []
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", timeout=15.0
    ) as client:
        try:
            body = (await client.get("/api/simulations/active")).json()
            sims = len(body) if isinstance(body, list) else int(body.get("count") or 0)
        except httpx.HTTPError, ValueError, AttributeError:
            pass
        try:
            body = (await client.get("/api/lab-tasks")).json()
            rows = body.get("tasks") if isinstance(body, dict) else body
            tasks.extend(
                str(row.get("labName") or row.get("name") or row.get("id"))
                for row in rows or []
                if isinstance(row, dict) and str(row.get("status", "")).lower() in LIVE_STATUSES
            )
        except httpx.HTTPError, ValueError, AttributeError:
            pass
    return sims, tasks


async def busy(service: AgentService) -> bool:
    sims, tasks = await work_in_flight(service)
    return bool(sims or tasks)


async def _running_work(service: AgentService) -> str:
    sims, tasks = await work_in_flight(service)
    lines = [f"simulations in flight: {sims}"]
    if tasks:
        lines.append(f"lab tasks running: {len(tasks)} ({', '.join(tasks[:6])})")
    return "\n".join(lines) if (sims or tasks) else "nothing running"


def _parse(raw: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    verdict = str(data.get("verdict", "")).lower().strip()
    if verdict not in {"met", "continue", "wait", "blocked", "impossible"}:
        return None
    return data


async def _judge(service: AgentService, goal: Goal, thread: Thread) -> dict[str, Any] | None:
    budget = goal.remaining("brain_simulations")
    user = (
        f"Goal: {goal.objective}\n"
        + (f"Met when: {goal.done_when}\n" if goal.done_when else "")
        + f"Turn {goal.turns} of {goal.max_turns}. Simulation budget left: "
        + ("no ceiling" if budget is None else str(budget))
        + f"\n\nWork running now:\n{await _running_work(service)}"
        + f"\n\nLab tasks this goal created:\n{await _goal_tasks(service, goal)}"
        + f"\n\nRecent transcript:\n{_transcript(thread)}\n\n"
        + "Verdict?"
    )
    try:
        answer = await service.state.llm.generate(system=JUDGE_SYSTEM, user=user, temperature=0.0)
    except Exception:
        log.warning("goal.judge_call_failed", exc_info=True)
        return None
    return _parse(answer.text)


async def _goal_tasks(service: AgentService, goal: Goal) -> str:
    """The goal's own lab tasks and where each one stands. An IDLE task was added but never
    started: it spends and progresses only after ``POST /api/lab-tasks/{id}/run``."""
    if not goal.task_ids:
        return "none yet"
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", timeout=15.0
    ) as c:
        try:
            tasks = (await c.get("/api/lab-tasks")).json().get("tasks") or []
        except httpx.HTTPError, ValueError, AttributeError:
            return "unknown"
    lines = []
    for t in tasks:
        if t.get("id") in goal.task_ids:
            status = str(t.get("status"))
            note = " (added but NOT started: run it)" if status == "IDLE" else ""
            lines.append(
                f"#{t['id']} {t.get('labName')}: {status}{note}, "
                f"{t.get('simulated', 0)} of {t.get('target', 0)} simulated"
            )
    return "\n".join(lines) or "none found"


async def _pause_tasks(service: AgentService, goal: Goal) -> None:
    """The budget is spent: stop the goal's own tasks from queueing more simulations."""
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        headers={"x-harness-client": "1"},
        timeout=15.0,
    ) as c:
        for task_id in goal.task_ids:
            try:
                await c.post(f"/api/lab-tasks/{task_id}/pause")
            except httpx.HTTPError:
                log.warning("goal.pause_task_failed", task=task_id)


def _done_when(goal: Goal) -> str:
    return f"Met when: {goal.done_when}\n" if goal.done_when else ""


async def step(service: AgentService, thread_id: int | None) -> dict[str, Any]:
    """Decide what the loop does after a turn. Returns an ``action`` for the panel:
    ``continue`` (send ``prompt``), ``wait`` (send ``prompt`` after ``seconds``),
    ``await_approval``, ``stop``, or ``none`` when no goal is running."""
    goal = service.goals.goal
    if goal is None or not goal.running:
        return {"action": "none", "goal": goal.to_dict() if goal else None}
    thread = service.threads.get(thread_id) if thread_id is not None else None
    if thread is None:
        return {"action": "none", "goal": goal.to_dict()}
    goal.thread_id = thread.id

    # An approval card is waiting on the person: the loop cannot move until they decide.
    if any(p.thread_id == thread.id for p in service.gate.pending()):
        goal.note("await_approval", "Waiting for your Approve or Reject on the card above.")
        return {"action": "await_approval", "goal": goal.to_dict()}

    goal.turns += 1
    goal.idle_turns = 0 if thread.last_actions else goal.idle_turns + 1
    # Spend is what the harness actually launched, whatever the requests claimed.
    actual = await costs.spent_since(service.state, goal.baseline_used)
    goal.spent["brain_simulations"] = max(goal.spent.get("brain_simulations", 0), actual)

    # Hard stops, checked in code so no verdict can talk past them.
    left = goal.remaining("brain_simulations")
    if left is not None and left <= 0:
        goal.end("spent", "the simulation budget is used up")
    elif goal.turns >= goal.max_turns:
        goal.end("turns_out", f"reached the {goal.max_turns}-turn limit without meeting the goal")
    elif goal.idle_turns >= STALL_TURNS:
        goal.end("stalled", f"{goal.idle_turns} turns in a row without taking any action")
    if not goal.running:
        if goal.status == "spent":
            await _pause_tasks(service, goal)
        return {"action": "stop", "goal": goal.to_dict()}

    verdict = await _judge(service, goal, thread)
    if verdict is None:
        goal.judge_failures += 1
        if goal.judge_failures >= MAX_JUDGE_FAILURES:
            goal.status = "paused"
            goal.note(
                "paused", "The judge could not be read several times in a row. Resume to retry."
            )
            return {"action": "stop", "goal": goal.to_dict()}
        verdict = {
            "verdict": "continue",
            "reason": "The judge gave no readable verdict; carrying on.",
        }
    else:
        goal.judge_failures = 0

    kind = verdict["verdict"]
    reason = str(verdict.get("reason") or "").strip() or "no reason given"
    log.info("goal.verdict", turn=goal.turns, verdict=kind, reason=reason[:160])

    if kind in {"met", "impossible", "blocked"}:
        goal.end(kind, reason)
        return {"action": "stop", "goal": goal.to_dict()}
    if kind == "wait":
        try:
            seconds = int(verdict.get("wait_seconds") or WAIT_DEFAULT)
        except TypeError, ValueError:
            seconds = WAIT_DEFAULT
        seconds = max(WAIT_MIN, min(WAIT_MAX, seconds))
        goal.status = "waiting"
        goal.wait_until = time.time() + seconds
        goal.note("wait", reason)
        prompt = RESUME_PROMPT.format(
            objective=goal.objective, done_when=_done_when(goal), reason=reason
        )
        return {"action": "wait", "seconds": seconds, "prompt": prompt, "goal": goal.to_dict()}

    goal.note("continue", reason)
    prompt = CONTINUE_PROMPT.format(
        turn=goal.turns + 1,
        max_turns=goal.max_turns,
        objective=goal.objective,
        done_when=_done_when(goal),
        reason=reason,
    )
    return {"action": "continue", "prompt": prompt, "goal": goal.to_dict()}
