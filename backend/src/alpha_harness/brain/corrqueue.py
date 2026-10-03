"""One correlation job at a time, for the whole account.

BRAIN runs a single correlation job per account. Self, production and Power Pool
correlations, and the correlation step of an Alpha's submission checks, all share it. Two
reads at once do not finish sooner: each keeps answering ``Retry-After`` until the other is
done, and both can time out. A submit started in the BRAIN web UI holds the same slot, which
is why a read can sit pending for an hour with nothing wrong on this side.

So every such read in this app goes through this queue:

- **Serial.** One of this app's jobs is asked of BRAIN at a time. The rest wait here, where
  they can be seen. (BRAIN cannot be told to drop a job, so a job this app gave up on may
  still occupy the slot for a while; the held message says so.)
- **Shared.** Asking for a job that is already queued or running joins it instead of adding
  a second copy, and a finished answer is kept for ``keep_for`` seconds, so asking again
  after giving up collects it instead of starting over.
- **Bounded for callers.** A caller waits at most ``max_wait`` seconds, queue time included,
  then gets :class:`BrainPollTimeout`. The job itself keeps its place: a caller leaving
  never abandons it.
- **Honest about waiting.** The running job reports how long BRAIN has kept it pending and
  what it last said. Past ``held_after`` seconds the queue says the slot looks held.
- **Timeouts are not measurements.** A check whose correlation came back WARNING or ERROR
  with no value was almost always never computed. :func:`correlation_readings` labels it so,
  because read as a real WARNING it condemns an Alpha that may be well under the limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import structlog

from ..realtime import TOPIC_CORRELATIONS
from .client import DEFAULT_VERSION, MIN_POLL_DELAY
from .errors import BrainPollTimeout

if TYPE_CHECKING:
    from ..realtime import Hub
    from .client import BrainClient

log = structlog.get_logger(__name__)

JobKind = Literal["self", "prod", "power-pool", "check"]
Outcome = Literal["done", "timeout", "error", "cancelled"]

#: How many finished jobs the snapshot keeps, newest first.
HISTORY = 20


def _path(kind: JobKind, alpha_id: str) -> str:
    if kind == "check":
        return f"/alphas/{alpha_id}/check"
    return f"/alphas/{alpha_id}/correlations/{kind}"


@dataclass
class Job:
    kind: JobKind
    alpha_id: str
    queued_at: float
    future: asyncio.Future[dict[str, Any]]
    started_at: float | None = None
    started_wall: float | None = None
    finished_at: float | None = None
    polls: int = 0
    last_retry_after: float | None = None
    outcome: Outcome | None = None
    error: str | None = None
    #: Callers currently waiting on this job.
    waiters: int = 0

    @property
    def key(self) -> tuple[JobKind, str]:
        return (self.kind, self.alpha_id)


def _settle(future: asyncio.Future[dict[str, Any]]) -> None:
    """Mark a finished future's exception as retrieved, so an answer nobody collected is not
    reported as an error at garbage collection."""
    if future.done() and not future.cancelled():
        future.exception()


@dataclass
class CorrelationQueue:
    """Serialises BRAIN's correlation and check jobs across every caller in the app."""

    client: BrainClient
    hub: Hub | None = None
    #: Seconds one job may stay pending at BRAIN before it is abandoned as a timeout.
    job_timeout: float = 300.0
    #: Seconds a caller waits, queue time included, before it is told to try again.
    max_wait: float | None = None
    #: Seconds pending after which the slot is reported as held.
    held_after: float = 180.0
    #: Seconds a finished answer is kept for anyone asking the same job again.
    keep_for: float = 600.0
    clock: Any = time.monotonic
    wall: Any = time.time
    sleep: Any = asyncio.sleep

    _pending: deque[Job] = field(default_factory=deque, init=False)
    _jobs: dict[tuple[JobKind, str], Job] = field(default_factory=dict, init=False)
    _running: Job | None = field(default=None, init=False)
    _history: deque[Job] = field(default_factory=lambda: deque(maxlen=HISTORY), init=False)
    _kept: dict[tuple[JobKind, str], tuple[float, dict[str, Any]]] = field(
        default_factory=dict, init=False
    )
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _worker: asyncio.Task[None] | None = field(default=None, init=False)

    # -- lifecycle -----------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work(), name="correlation-queue")

    async def stop(self) -> None:
        """Stop the worker and release every caller still waiting, queued or running."""
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            # A worker that died of its own error must not abort the app's shutdown.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker
        self.clear()

    def clear(self) -> int:
        """Cancel every queued job (not the running one). Returns how many were dropped."""
        dropped = 0
        while self._pending:
            job = self._pending.popleft()
            self._finish(job, "cancelled", "cleared from the queue")
            if not job.future.done():
                job.future.cancel()
            dropped += 1
        return dropped

    # -- the one public verb -------------------------------------------------

    async def run(self, kind: JobKind, alpha_id: str) -> dict[str, Any]:
        """BRAIN's answer for this job, waiting behind whatever is ahead of it.

        Raises :class:`BrainPollTimeout` after ``max_wait`` seconds; the job stays queued and
        its answer is kept, so asking again later collects it.
        """
        key = (kind, alpha_id)
        kept = self._kept.get(key)
        if kept is not None and self.clock() - kept[0] <= self.keep_for:
            return kept[1]
        job = self._jobs.get(key)
        if job is None:
            job = Job(kind, alpha_id, self.clock(), asyncio.get_running_loop().create_future())
            self._jobs[key] = job
            self._pending.append(job)
            self._wake.set()
            await self._publish()
        self._ensure_worker()
        return await self._wait(job)

    async def _wait(self, job: Job) -> dict[str, Any]:
        """Await the job through a private future, so a caller leaving cannot cancel it."""
        waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        def relay(done: asyncio.Future[dict[str, Any]]) -> None:
            if waiter.done():
                return
            if done.cancelled():
                waiter.cancel()
            elif (exc := done.exception()) is not None:
                waiter.set_exception(exc)
            else:
                waiter.set_result(done.result())

        job.waiters += 1
        job.future.add_done_callback(relay)
        try:
            if self.max_wait is None:
                return await waiter
            try:
                return await asyncio.wait_for(waiter, self.max_wait)
            except TimeoutError:
                raise BrainPollTimeout(
                    f"{job.kind} for {job.alpha_id} waited {round(self.max_wait)} s in the "
                    "correlation queue. It keeps its place; ask again later to collect the "
                    "answer. BRAIN runs one correlation job per account."
                ) from None
        finally:
            job.waiters = max(0, job.waiters - 1)
            job.future.remove_done_callback(relay)

    # -- the worker ----------------------------------------------------------

    def _finish(self, job: Job, outcome: Outcome, error: str | None = None) -> None:
        job.outcome, job.error = outcome, error
        job.finished_at = self.clock()
        if self._running is job:
            self._running = None
        if self._jobs.get(job.key) is job:
            del self._jobs[job.key]
        self._history.appendleft(job)

    async def _work(self) -> None:
        while True:
            while not self._pending:
                self._wake.clear()
                await self._wake.wait()
            job = self._pending.popleft()
            self._running = job
            job.started_at, job.started_wall = self.clock(), self.wall()
            await self._publish()
            try:
                body = await self._drive(job)
            except asyncio.CancelledError:
                # Stopping mid-poll: release this job's callers instead of leaving them
                # waiting on an answer that will never be set.
                self._finish(job, "cancelled", "the queue stopped while BRAIN was working")
                job.future.cancel()
                raise
            except BrainPollTimeout as exc:
                self._finish(job, "timeout", str(exc))
                job.future.set_exception(exc)
            except Exception as exc:  # noqa: BLE001 - every failure belongs to the caller
                self._finish(job, "error", f"{type(exc).__name__}: {exc}")
                job.future.set_exception(exc)
            else:
                self._finish(job, "done")
                self._kept[job.key] = (self.clock(), body)
                job.future.set_result(body)
            _settle(job.future)
            self._prune()
            await self._publish()

    def _prune(self) -> None:
        now = self.clock()
        for key in [k for k, (at, _) in self._kept.items() if now - at > self.keep_for]:
            del self._kept[key]

    async def _drive(self, job: Job) -> dict[str, Any]:
        """Poll one job to completion, recording what BRAIN says on every round."""
        path = _path(job.kind, job.alpha_id)
        started = job.started_at if job.started_at is not None else self.clock()
        deadline = started + self.job_timeout
        announced_held = False
        while True:
            response = await self.client.request_retrying("GET", path, version=DEFAULT_VERSION)
            job.polls += 1
            if not response.pending:
                return response.body if isinstance(response.body, dict) else {}
            job.last_retry_after = response.retry_after
            now = self.clock()
            if not announced_held and now - started >= self.held_after:
                announced_held = True
                log.info("correlation_queue.held", kind=job.kind, alpha=job.alpha_id)
                await self._publish()
            remaining = deadline - now
            if remaining <= 0:
                raise BrainPollTimeout(
                    f"{job.kind} for {job.alpha_id} still pending after "
                    f"{round(now - started)} s and {job.polls} polls. BRAIN runs one "
                    "correlation job per account; it may be held by a submit elsewhere."
                )
            await self.sleep(min(max(response.retry_after or 0.0, MIN_POLL_DELAY), remaining))

    # -- what anyone can see -------------------------------------------------

    def _describe(self, job: Job, now: float) -> dict[str, Any]:
        began = job.started_at or job.finished_at or now
        entry: dict[str, Any] = {
            "kind": job.kind,
            "alphaId": job.alpha_id,
            "queuedSeconds": round(began - job.queued_at, 1),
        }
        if job.outcome is None:
            entry["waiters"] = job.waiters
        if job.started_at is not None:
            end = job.finished_at if job.finished_at is not None else now
            entry["pendingSeconds"] = round(end - job.started_at, 1)
            entry["polls"] = job.polls
            entry["startedAt"] = job.started_wall
        if job.last_retry_after is not None:
            entry["lastRetryAfter"] = job.last_retry_after
        if job.outcome is not None:
            entry["outcome"] = job.outcome
        if job.error is not None:
            entry["error"] = job.error
        return entry

    def status(self) -> dict[str, Any]:
        now = self.clock()
        running = self._running
        started = running.started_at if running is not None else None
        held = started is not None and now - started >= self.held_after
        message = None
        if held and running is not None and started is not None:
            minutes = max(1, round((now - started) / 60))
            recent_timeout = any(
                j.outcome == "timeout"
                and j.finished_at is not None
                and now - j.finished_at <= self.job_timeout
                for j in self._history
            )
            cause = (
                "a job this app gave up on a few minutes ago that BRAIN is still finishing, "
                "a submit in the BRAIN web UI, or another session"
                if recent_timeout
                else "a submit in the BRAIN web UI or another session"
            )
            message = (
                f"BRAIN has kept this {running.kind} job pending for about {minutes} min. The "
                f"account runs one correlation job at a time, so the slot is probably held by "
                f"{cause}. Nothing here is stuck; the job runs when the slot frees."
            )
        return {
            "running": self._describe(running, now) if running else None,
            "queued": [self._describe(job, now) for job in self._pending],
            "recent": [self._describe(job, now) for job in self._history],
            "held": held,
            "message": message,
            "heldAfterSeconds": self.held_after,
            "jobTimeoutSeconds": self.job_timeout,
            "maxWaitSeconds": self.max_wait,
        }

    async def _publish(self) -> None:
        if self.hub is None:
            return
        with contextlib.suppress(Exception):
            await self.hub.broadcast(TOPIC_CORRELATIONS, self.status())


# -- reading BRAIN's correlation checks truthfully ------------------------------------


def is_unmeasured(check: dict[str, Any]) -> bool:
    """A correlation check BRAIN reported as WARNING or ERROR without computing a value."""
    return (
        "CORRELATION" in str(check.get("name") or "")
        and check.get("value") is None
        and check.get("result") in {"WARNING", "ERROR"}
    )


def correlation_readings(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Each correlation check in a ``/check`` answer, labelled by whether a number exists.

    BRAIN reports a correlation it never finished computing as WARNING or ERROR with no
    value. That is not a high correlation: it is no reading, and the fix is to read
    ``/correlations/{kind}?refresh=true`` again, not to reshape the Alpha.
    """
    checks = (body.get("is") or {}).get("checks") or []
    readings: list[dict[str, Any]] = []
    for check in checks:
        name = str(check.get("name") or "")
        if "CORRELATION" not in name:
            continue
        result = check.get("result")
        value = check.get("value")
        note = None
        if value is not None:
            reading = "measured"
        elif result == "PENDING":
            reading = "pending"
            note = "BRAIN is still computing this. It shares one job slot per account."
        elif is_unmeasured(check):
            reading = "unmeasured"
            note = (
                f"{result} with no value: BRAIN almost always means it did not finish "
                "computing it, because the account's one correlation slot was busy. Read it "
                "again with refresh; do not treat it as a high correlation."
            )
        elif result == "PASS":
            reading = "no-value"
            note = "PASS with no number, so a numeric bar cannot be checked against it."
        else:
            reading = "unknown"
        entry: dict[str, Any] = {"name": name, "result": result, "value": value}
        entry["reading"] = reading
        if check.get("limit") is not None:
            entry["limit"] = check.get("limit")
        if note:
            entry["note"] = note
        readings.append(entry)
    return readings
