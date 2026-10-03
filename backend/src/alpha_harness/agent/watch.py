"""Watches: Vision monitors work it chose to monitor, and is woken when that work ends.

Modelled on Claude Code's background monitors. Outside a goal, a turn ends when Vision stops
talking, so a lab task it started would sit finished until the person typed again. With a
watch, Vision says what it is waiting on and what it will do next; when the work finishes (or
the wait runs out) the backend runs a follow-up turn in the same conversation with the result
and Vision's own plan.

Vision decides what is worth watching. Nothing is watched automatically, and the guards keep a
watch from turning into an unattended loop:

- at most :data:`MAX_ACTIVE` watches at once;
- at most :data:`MAX_CHAIN` follow-ups in a row without a message from the person, after which
  a watch still reports but no longer runs a turn;
- not inside a running goal, whose loop already waits on work;
- every watch has a deadline, and a timed-out watch fires with that said plainly.

Watches persist in ``vision_watches.json``, so a restart rechecks them instead of losing them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

log = structlog.get_logger(__name__)

MAX_ACTIVE = 5
MAX_CHAIN = 5
DEFAULT_WAIT_MINUTES = 120
MAX_WAIT_MINUTES = 720
#: Harness topics that mean work moved; any of them triggers a recheck.
WAKE_TOPICS = frozenset({"simulations", "tasks", "studies", "sync"})
RECHECK_SECONDS = 60.0
DEBOUNCE_SECONDS = 3.0

FOLLOW_UP = """\
[A watch you set has fired]
{what}: {summary}
You planned: {then}
Do that now and report what it returned."""


@dataclass
class Watch:
    id: int
    thread_id: int
    kind: str  # "task" or "simulations"
    then: str
    deadline: float
    task_id: int | None = None
    created_at: float = 0.0

    @property
    def what(self) -> str:
        return f"Task {self.task_id}" if self.kind == "task" else "Simulations in flight"

    def view(self, now: float) -> dict[str, Any]:
        return {
            **asdict(self),
            "what": self.what,
            "minutesLeft": max(0, round((self.deadline - now) / 60)),
        }


class Watcher:
    def __init__(
        self,
        path: Path,
        *,
        status: Callable[[Watch], Awaitable[tuple[bool, str]]],
        follow_up: Callable[[Watch, str], Awaitable[None]],
        announce: Callable[[str], None],
        goal_running: Callable[[int], bool],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._status = status
        self._follow_up = follow_up
        self._announce = announce
        self._goal_running = goal_running
        self._clock = clock
        self.watches: list[Watch] = []
        #: Follow-ups run per conversation since the person last spoke.
        self.chain: dict[int, int] = {}
        self._next = 1
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        try:
            data = json.loads(path.read_text())
            self.watches = [Watch(**w) for w in data.get("watches", [])]
            self._next = int(data.get("next", 1))
        except OSError, ValueError, TypeError:
            pass

    # -- Vision's tools ------------------------------------------------------

    def add(self, thread_id: int, args: dict[str, Any]) -> dict[str, Any]:
        then = str(args.get("then") or "").strip()
        if not then:
            return {"error": "say in `then` what you will do when it finishes"}
        if self._goal_running(thread_id):
            return {"error": "a goal is running here; its loop already waits on work"}
        if len(self.watches) >= MAX_ACTIVE:
            return {"error": f"{MAX_ACTIVE} watches are already active; cancel one first"}
        task_id = args.get("task_id")
        if task_id not in (None, ""):
            try:
                task = int(task_id)
            except TypeError, ValueError:
                return {"error": "task_id must be a lab task id"}
            kind: str = "task"
        elif args.get("simulations"):
            task, kind = None, "simulations"
        else:
            return {"error": "give a task_id, or simulations: true"}
        try:
            minutes = int(args.get("max_wait_minutes") or DEFAULT_WAIT_MINUTES)
        except TypeError, ValueError:
            minutes = DEFAULT_WAIT_MINUTES
        minutes = max(1, min(MAX_WAIT_MINUTES, minutes))
        now = self._clock()
        watch = Watch(
            id=self._next,
            thread_id=thread_id,
            kind=kind,
            then=then[:500],
            deadline=now + minutes * 60,
            task_id=task,
            created_at=now,
        )
        self._next += 1
        self.watches.append(watch)
        self._save()
        self.start()
        log.info("watch.added", id=watch.id, kind=kind, task=task, minutes=minutes)
        return {
            "watching": watch.view(now),
            "note": "You will be woken in this conversation when it finishes. Tell the person "
            "what you are waiting for; do not ask them to poll.",
            "active": [w.view(now) for w in self.watches],
        }

    def cancel(self, watch_id: int) -> bool:
        before = len(self.watches)
        self.watches = [w for w in self.watches if w.id != watch_id]
        if len(self.watches) != before:
            self._save()
            return True
        return False

    def active(self, thread_id: int | None = None) -> list[dict[str, Any]]:
        now = self._clock()
        return [w.view(now) for w in self.watches if thread_id in (None, w.thread_id)]

    def person_spoke(self, thread_id: int) -> None:
        self.chain[thread_id] = 0

    # -- checking ------------------------------------------------------------

    def on_hub(self, topic: str, _payload: Any) -> None:
        if topic in WAKE_TOPICS and self.watches:
            self._wake.set()

    async def check_once(self) -> list[Watch]:
        """Fire every watch whose work has finished or whose deadline passed."""
        fired: list[Watch] = []
        for watch in list(self.watches):
            now = self._clock()
            try:
                finished, summary = await self._status(watch)
            except Exception:
                log.warning("watch.status_failed", id=watch.id, exc_info=True)
                finished, summary = False, ""
            timed_out = not finished and now >= watch.deadline
            if not (finished or timed_out):
                continue
            self.watches = [w for w in self.watches if w.id != watch.id]
            self._save()
            if timed_out:
                summary = f"still not finished after the wait ran out. Last seen: {summary}"
            await self._fire(watch, summary)
            fired.append(watch)
        return fired

    async def _fire(self, watch: Watch, summary: str) -> None:
        if self._goal_running(watch.thread_id):
            self._announce(
                f"Watch #{watch.id} ({watch.what}) ended: {summary} The goal loop has it."
            )
            return
        runs = self.chain.get(watch.thread_id, 0)
        if runs >= MAX_CHAIN:
            self._announce(
                f"Watch #{watch.id} ({watch.what}) ended: {summary} Not following up: "
                f"{MAX_CHAIN} follow-ups in a row without you. Send a message to carry on."
            )
            return
        self.chain[watch.thread_id] = runs + 1
        self._announce(f"Watch #{watch.id} ({watch.what}) ended: {summary} Vision is following up.")
        prompt = FOLLOW_UP.format(what=watch.what, summary=summary, then=watch.then)
        await self._follow_up(watch, prompt)

    # -- the loop ------------------------------------------------------------

    def start(self) -> None:
        if self.watches and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        try:
            while self.watches:
                await self.check_once()
                if not self.watches:
                    return
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=RECHECK_SECONDS)
                await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("watch.loop_failed", exc_info=True)

    def _save(self) -> None:
        data = {"next": self._next, "watches": [asdict(w) for w in self.watches]}
        try:
            self._path.write_text(json.dumps(data, indent=1))
        except OSError:
            log.warning("watch.save_failed", path=str(self._path), exc_info=True)
