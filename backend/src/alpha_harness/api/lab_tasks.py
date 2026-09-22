"""Tasks: what the labs added, run from one place.

A lab only adds a task. Here a task runs: it waits until its cores fit in the free slots,
runs until its simulations are spent, across days if it has to, and can be paused,
stopped, changed or removed. Search Lab, Template Lab and Evolution Lab add scheduler.
"""

from __future__ import annotations

import contextlib
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from ..db.models import SimStatus, SimulationRecord, Study, StudyStatus, Trial, TrialState, utcnow
from ..labs import scheduler, search
from ..labs.objectives import OBJECTIVES, StudyNotFoundError
from ..labs.params import TASK_SAMPLERS, TEMPLATE_SAMPLER, task_params
from ..labs.study import ranked
from ..schemas import Out
from ..vault.yields import checks_of, verdict
from .deps import State, refuse

router = APIRouter(prefix="/api/lab-tasks", tags=["lab-tasks"])

#: Where a task can be run from: never started, paused, or failed. A failed task may have
#: sent simulations that finished after it stopped; running it again scores them.
RUNNABLE = (StudyStatus.IDLE, StudyStatus.PAUSED, StudyStatus.FAILED)
FINISHED = (StudyStatus.COMPLETE, StudyStatus.FAILED)


class TaskChange(BaseModel):
    #: Up to the engine's slots, checked in the route: a lab starts at ``search.MAX_CORES``
    #: at most, but an edit may hand it the whole engine.
    cores: int | None = Field(default=None, ge=1)
    simulations: int | None = Field(default=None, ge=1, le=search.MAX_SIMULATIONS)


class LabTask(Out):
    id: int
    lab: str
    lab_name: str
    #: Template Lab only: the template's name and its skeleton.
    template_name: str | None
    template: str | None
    #: IDLE: not started. QUEUED: run, waiting for its cores to fit in the free slots.
    status: StudyStatus
    stopping: bool
    message: str | None
    region: str | None
    delay: int | None
    #: Settings Sampler only: what the sweep holds fixed, and where it came from.
    alpha_id: str | None = None
    markets: int | None = None
    truncation: float | None = None
    nan_handling: str | None = None
    #: Evolution Lab only: its market's universe, seeds, population and mutation rate.
    universe: str | None
    seeds: int
    population: int | None
    mutation_rate: float | None
    #: Search Lab and Template Lab only.
    decay: int | None
    cores: int
    dataset_ids: list[str]
    fields: int
    target: int
    simulated: int
    #: Of ``simulated``, how many came back from the dedup cache without spending quota.
    cached: int
    queued: int
    running: int
    failed: int
    #: The best value of what the task searches for: Sharpe, or Train Fitness for Evolution Lab.
    best: float | None
    objective_label: str
    created_at: str | None
    #: When it first began running, which is where elapsed time is measured from.
    started_at: str | None
    finished_at: str | None


class LabTasks(Out):
    slots: int
    tasks: list[LabTask]


class TaskRemoved(Out):
    removed: int


class RankedAlpha(Out):
    trial_id: int
    #: Its place in the sweep, as queued.
    number: int
    alpha_id: str | None
    expression: str | None
    settings: dict[str, Any] | None
    value: float
    sharpe: float | None
    fitness: float | None
    turnover: float | None
    returns: float | None
    drawdown: float | None
    margin: float | None
    #: From the vault, which holds every simulated Alpha; a trial's own stats may lack them.
    long_count: int | None = None
    short_count: int | None = None
    feasible: bool | None
    failed_checks: list[str]
    #: Nothing BRAIN has reported refuses it: no FAIL and no ERROR.
    submittable: bool
    #: Submittable only because a check has not answered yet. The Submission Planner waits for
    #: these rather than planning a permanent submission on them.
    pending: bool = False
    #: The Alpha the sweep started from, kept first as its reference point.
    source: bool = False


async def _rows(state: Any) -> list[Study]:
    async with state.db.session() as session:
        return list(
            (
                await session.scalars(
                    select(Study).where(Study.sampler.in_(TASK_SAMPLERS)).order_by(Study.id.desc())
                )
            ).all()
        )


async def _one(state: Any, task_id: int) -> Study:
    row = await state.optimizer.get(task_id)
    if row is None or row.sampler not in TASK_SAMPLERS:
        raise StudyNotFoundError(task_id)
    return row


async def _progress(state: Any, ids: list[int]) -> dict[int, dict[str, Any]]:
    """Trials by state, trials answered for free, and the best value per task, counted in SQL.

    A long task holds tens of thousands of trials; reading them all to count them would
    slow the list down with every day the task runs.
    """
    out: dict[int, dict[str, Any]] = {i: {"states": {}, "free": 0, "best": None} for i in ids}
    if not ids:
        return out
    value = func.json_extract(Trial.values, "$[0]")
    async with state.db.session() as session:
        states = await session.execute(
            select(Trial.study_id, Trial.state, func.count())
            .where(Trial.study_id.in_(ids))
            .group_by(Trial.study_id, Trial.state)
        )
        for study_id, trial_state, n in states.all():
            out[study_id]["states"][str(trial_state)] = int(n)
        free = await session.execute(
            select(Trial.study_id, func.count())
            .where(
                Trial.study_id.in_(ids),
                Trial.state.in_([TrialState.COMPLETE, TrialState.FAIL]),
                Trial.message == scheduler.FREE,
            )
            .group_by(Trial.study_id)
        )
        for study_id, n in free.all():
            out[study_id]["free"] = int(n)
        # An Alpha that returned no value is scored at the failure value, so it is not a best;
        # nor is a seed, which was scored before the task began.
        best = await session.execute(
            select(Trial.study_id, func.max(value))
            .where(
                Trial.study_id.in_(ids),
                Trial.state == TrialState.COMPLETE,
                value > OBJECTIVES["sharpe"].failure_value,
                or_(Trial.generation.is_(None), Trial.generation != 0),
            )
            .group_by(Trial.study_id)
        )
        for study_id, value in best.all():
            out[study_id]["best"] = None if value is None else float(value)
    return out


def _task(row: Study, progress: dict[str, Any]) -> LabTask:
    # Read raw on purpose: this only displays, and one malformed old row must not take the
    # whole Tasks list down with a validation error.
    params = row.sampler_params or {}
    states = progress["states"]
    told = states.get(TrialState.COMPLETE, 0) + states.get(TrialState.FAIL, 0)
    template = row.sampler == TEMPLATE_SAMPLER
    objective = OBJECTIVES.get((row.objectives or ["sharpe"])[0], OBJECTIVES["sharpe"])
    return LabTask.model_validate(
        {
            "id": row.id,
            "lab": row.sampler,
            "labName": TASK_SAMPLERS.get(row.sampler, row.sampler),
            "templateName": row.template_name if template else None,
            "template": row.template_source if template else None,
            "status": row.status,
            "stopping": bool(params.get("stopping")),
            "message": row.message,
            "region": params.get("region"),
            "delay": params.get("delay"),
            "universe": params.get("universe"),
            "seeds": len(params.get("seeds") or []),
            "population": params.get("population"),
            "mutationRate": params.get("mutationRate"),
            "decay": params.get("decay"),
            "alphaId": params.get("alphaId"),
            "markets": params.get("markets"),
            "truncation": params.get("truncation"),
            "nanHandling": params.get("nanHandling"),
            "cores": scheduler.cores_of(row),
            "datasetIds": params.get("datasetIds") or [],
            "fields": len((params.get("space") or {}).get("fields") or {}),
            "target": row.max_trials,
            # What spent quota, as the scheduler counts toward the target.
            "simulated": told - progress["free"],
            "cached": progress["free"],
            "queued": states.get(TrialState.QUEUED, 0),
            "running": states.get(TrialState.RUNNING, 0),
            "failed": states.get(TrialState.FAIL, 0),
            "best": progress["best"],
            "objectiveLabel": objective.label,
            "createdAt": row.created_at.isoformat() if row.created_at else None,
            "startedAt": row.started_at.isoformat() if row.started_at else None,
            "finishedAt": row.finished_at.isoformat() if row.finished_at else None,
        }
    )


async def _payload(state: Any, task_id: int) -> LabTask:
    row = await _one(state, task_id)
    return _task(row, (await _progress(state, [row.id]))[row.id])


@router.get("")
async def list_tasks(state: State) -> LabTasks:
    """Every task, newest first, and the slots they share."""
    rows = await _rows(state)
    progress = await _progress(state, [r.id for r in rows])
    return LabTasks(slots=state.engine.slots, tasks=[_task(r, progress[r.id]) for r in rows])


async def _queue(state: Any, ids: list[int]) -> None:
    """Hand tasks to the scheduler. Each starts as soon as its cores fit."""
    queued_at = utcnow().isoformat()
    # The same lock ``remove`` holds, so a task cannot be queued while it is being deleted.
    async with contextlib.AsyncExitStack() as stack:
        for task_id in sorted(ids):
            await stack.enter_async_context(state.optimizer.lock(task_id))
        await _mark_queued(state, ids, queued_at)
    await scheduler.start_waiting(state.optimizer)
    await state.optimizer.notify()


async def _mark_queued(state: Any, ids: list[int], queued_at: str) -> None:
    async with state.db.session() as session:
        for task_id in ids:
            row = await session.get(Study, task_id)
            if row is not None and row.status in RUNNABLE:
                if row.status == StudyStatus.FAILED:
                    row.message = None
                    row.finished_at = None
                row.status = StudyStatus.QUEUED
                params = task_params(row)
                params.queued_at = queued_at
                row.sampler_params = params.dump()
        await session.commit()


@router.post("/run-all")
async def run_all(state: State) -> LabTasks:
    """Run every task not started yet, oldest first. What does not fit waits its turn."""
    fresh = sorted(r.id for r in await _rows(state) if r.status == StudyStatus.IDLE)
    await _queue(state, fresh)
    return await list_tasks(state)


@router.post("/{task_id}/run")
async def run(task_id: int, state: State) -> LabTask:
    """Run a task, resume a paused one, or retry a failed one. It starts once its cores fit."""
    row = await _one(state, task_id)
    if row.status not in RUNNABLE:
        raise refuse(409, "not_runnable", "Only a task not started, paused or failed can run.")
    await _queue(state, [task_id])
    return await _payload(state, task_id)


@router.post("/{task_id}/pause")
async def pause(task_id: int, state: State) -> LabTask:
    """Queue nothing more for now. Simulations already sent finish; the rest come off."""
    # Read under the lock: checked outside it, a task that finished its last trial in the
    # meantime was written back to PAUSED, and a finished task holds no cores.
    async with state.optimizer.lock(task_id):
        row = await _one(state, task_id)
        stopping = task_params(row).stopping
        if row.status not in (StudyStatus.RUNNING, StudyStatus.QUEUED) or stopping:
            raise refuse(409, "not_running", "Only a running or waiting task can pause.")
        await state.optimizer.set_status(task_id, StudyStatus.PAUSED)
        await state.engine.drop_queued(row.task)
        await scheduler.prune_unsent(state.optimizer, task_id)
    await scheduler.start_waiting(state.optimizer)
    return await _payload(state, task_id)


@router.post("/{task_id}/stop")
async def stop(task_id: int, state: State) -> LabTask:
    """Finish a task early. Simulations already sent still finish and are scored.

    Pressed on a task that is *already* stopping, it forces: what is out on BRAIN is
    cancelled where it can be, the trials close whatever their simulations are doing, and
    the cores come back. There is no separate button because there is no separate
    intention — the second press means the first one did not work.
    """
    row = await _one(state, task_id)
    if row.status not in (StudyStatus.RUNNING, StudyStatus.PAUSED, StudyStatus.QUEUED):
        raise refuse(409, "not_started", "Only a task that has been run can stop.")
    await scheduler.stop_task(state.optimizer, row, force=task_params(row).stopping)
    return await _payload(state, task_id)


@router.patch("/{task_id}")
async def change(task_id: int, body: TaskChange, state: State) -> LabTask:
    """Change a task's cores or simulations. A running task takes them from its next round."""
    row = await _one(state, task_id)
    if row.status in FINISHED:
        raise refuse(409, "finished", "A finished task can't change.")
    if body.cores and body.cores > state.engine.slots:
        raise refuse(
            422,
            "too_many_cores",
            f"The engine has {state.engine.slots} slots, so a task cannot hold {body.cores}.",
        )
    cores = body.cores or scheduler.cores_of(row)
    free = await scheduler.resize_task(state.optimizer, row, cores, body.simulations)
    if free is not None:
        raise refuse(409, "no_cores", f"Only {free} more cores are free right now.")
    return await _payload(state, task_id)


@router.delete("/{task_id}")
async def remove(task_id: int, state: State) -> TaskRemoved:
    """Remove a task that is not running. The Alphas it found stay in Alphas."""
    # Held across the check and the delete: a ``run`` landing between them queued work for a
    # task whose rows were then deleted under it, leaving simulations nothing would score.
    async with state.optimizer.lock(task_id):
        row = await _one(state, task_id)
        # Asks the simulation rows, not the trials: a task that failed mid-round never scores
        # its last trials, so they read RUNNING forever after their simulations finished.
        async with state.db.session() as session:
            out = await session.scalar(
                select(func.count())
                .select_from(SimulationRecord)
                .where(
                    SimulationRecord.task == row.task,
                    SimulationRecord.status.in_([SimStatus.PENDING, SimStatus.RUNNING]),
                )
            )
        if row.status in (StudyStatus.RUNNING, StudyStatus.QUEUED) or out:
            raise refuse(409, "running", "Pause or stop the task first: simulations are still out.")
        await state.engine.drop_queued(row.task)
        await state.optimizer.delete(task_id)
        await state.engine.set_quota(row.task, 0, enabled=False)
    await state.optimizer.notify()
    return TaskRemoved(removed=task_id)


@router.get("/{task_id}/top")
async def top(
    task_id: int,
    state: State,
    # A sweep's whole result set is worth scrolling, so the ceiling is a day's
    # simulations rather than a page; the caller asks for what it means to show.
    limit: Annotated[int, Query(ge=1, le=5000)] = 20,
) -> list[RankedAlpha]:
    """The task's best Alphas on what it searches for. Seeds are not among them."""
    row = await _one(state, task_id)
    async with state.db.session() as session:
        best = list(
            (
                await session.scalars(
                    select(Trial)
                    .where(
                        Trial.study_id == task_id,
                        Trial.state == TrialState.COMPLETE,
                        or_(Trial.generation.is_(None), Trial.generation != 0),
                    )
                    .order_by(func.json_extract(Trial.values, "$[0]").desc())
                    .limit(limit)
                )
            ).all()
        )
    stored = await state.alphas.by_ids([t.alpha_id for t in best if t.alpha_id])
    current = {a: checks_of(r.get("checks")) for a, r in stored.items() if r.get("checks")}
    return [
        RankedAlpha.model_validate(
            r
            | {
                "longCount": (stored.get(r["alphaId"]) or {}).get("long_count"),
                "shortCount": (stored.get(r["alphaId"]) or {}).get("short_count"),
            }
        )
        for r in ranked(best, row.directions, current)
    ]


class TaskAlpha(RankedAlpha):
    """A submittable Alpha, with the task that found it."""

    task_id: int
    task_name: str


@router.get("/submittable")
async def submittable_alphas(state: State) -> list[TaskAlpha]:
    """Every Alpha from every task that nothing BRAIN reports refuses: each check PASS, WARNING
    or PENDING, apart from the ones that never gate (``vault.yields.IGNORED_CHECKS``).

    Judged on the vault's checks where it has them, as a task's own list is. Each Alpha once,
    from the task that found it first.
    """
    tasks = {row.id: row for row in await _rows(state)}
    async with state.db.session() as session:
        trials = list(
            (
                await session.scalars(
                    select(Trial)
                    .where(
                        Trial.study_id.in_(list(tasks)),
                        Trial.state == TrialState.COMPLETE,
                        Trial.alpha_id.is_not(None),
                        or_(Trial.generation.is_(None), Trial.generation != 0),
                    )
                    .order_by(Trial.study_id, Trial.number)
                )
            ).all()
        )
    # A check that FAILed at simulation time stays failed: BRAIN's later checks only settle
    # what was PENDING. Dropping those first spares looking most Alphas up in the vault.
    trials = [t for t in trials if verdict((t.result or {}).get("checks") or []) != "refused"]
    stored = await state.alphas.by_ids(list({str(t.alpha_id) for t in trials}))
    current = {a: checks_of(r.get("checks")) for a, r in stored.items() if r.get("checks")}
    by_trial = {t.id: t.study_id for t in trials}
    out: list[TaskAlpha] = []
    seen: set[str] = set()
    for r in ranked(trials, None, current):
        alpha_id = str(r["alphaId"])
        if not r["submittable"] or alpha_id in seen:
            continue
        seen.add(alpha_id)
        task = tasks[by_trial[r["trialId"]]]
        vault = stored.get(alpha_id) or {}
        out.append(
            TaskAlpha.model_validate(
                r
                | {
                    "longCount": vault.get("long_count"),
                    "shortCount": vault.get("short_count"),
                    "taskId": task.id,
                    "taskName": task.template_name or TASK_SAMPLERS.get(task.sampler, task.sampler),
                }
            )
        )
    return out
