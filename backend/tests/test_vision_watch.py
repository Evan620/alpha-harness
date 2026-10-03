"""Watches: Vision monitors work it chose to, and a fired watch runs one follow-up turn.

The watcher is driven with a fake clock, a scripted status and a recording follow-up, so these
tests pin the rules without a harness: only finished or timed-out work fires, the plan comes
back in the prompt, the chain limit stops an unattended loop, and watches survive a restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from alpha_harness.agent.watch import MAX_ACTIVE, MAX_CHAIN, Watch, Watcher

if TYPE_CHECKING:
    from pathlib import Path


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.now = 1_000.0
        self.finished: dict[int, bool] = {}
        self.follow_ups: list[tuple[Watch, str]] = []
        self.notes: list[str] = []
        self.goal_thread: int | None = None
        self.path = tmp_path / "watches.json"

    def watcher(self) -> Watcher:
        async def status(w: Watch) -> tuple[bool, str]:
            done = self.finished.get(w.task_id or 0, False)
            return done, "COMPLETE, 1 of 1 simulated, best 1.4" if done else "RUNNING"

        async def follow_up(w: Watch, prompt: str) -> None:
            self.follow_ups.append((w, prompt))

        return Watcher(
            self.path,
            status=status,
            follow_up=follow_up,
            announce=self.notes.append,
            goal_running=lambda thread: thread == self.goal_thread,
            clock=lambda: self.now,
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def task_watch(w: Watcher, thread: int = 1, task: int = 36, **kw: Any) -> dict[str, Any]:
    w.start = lambda: None  # type: ignore[method-assign]  # tests drive check_once directly
    return w.add(thread, {"task_id": task, "then": "read its IS checks", **kw})


@pytest.mark.asyncio
async def test_finished_work_fires_once_with_the_plan(rig: Rig) -> None:
    w = rig.watcher()
    assert "watching" in task_watch(w)
    assert await w.check_once() == []  # still running, nothing fires
    rig.finished[36] = True
    fired = await w.check_once()
    assert [f.task_id for f in fired] == [36]
    (_watch, prompt) = rig.follow_ups[0]
    assert "Task 36: COMPLETE" in prompt and "You planned: read its IS checks" in prompt
    assert w.watches == []
    assert await w.check_once() == []  # fired watches are gone


@pytest.mark.asyncio
async def test_a_watch_times_out_and_says_so(rig: Rig) -> None:
    w = rig.watcher()
    task_watch(w, max_wait_minutes=10)
    rig.now += 11 * 60
    await w.check_once()
    assert "wait ran out" in rig.follow_ups[0][1]


@pytest.mark.asyncio
async def test_chain_limit_stops_an_unattended_loop(rig: Rig) -> None:
    w = rig.watcher()
    rig.finished[36] = True
    for _ in range(MAX_CHAIN):
        task_watch(w)
        await w.check_once()
    assert len(rig.follow_ups) == MAX_CHAIN
    task_watch(w)
    await w.check_once()
    assert len(rig.follow_ups) == MAX_CHAIN  # reported, but no turn
    assert "Not following up" in rig.notes[-1]
    w.person_spoke(1)
    task_watch(w)
    await w.check_once()
    assert len(rig.follow_ups) == MAX_CHAIN + 1


@pytest.mark.asyncio
async def test_no_watches_inside_a_running_goal(rig: Rig) -> None:
    w = rig.watcher()
    rig.goal_thread = 1
    assert "error" in task_watch(w)
    rig.goal_thread = None
    task_watch(w)
    rig.goal_thread = 1  # a goal started meanwhile: its loop takes over
    rig.finished[36] = True
    await w.check_once()
    assert rig.follow_ups == []
    assert "goal loop has it" in rig.notes[-1]


def test_add_validates_and_caps(rig: Rig) -> None:
    w = rig.watcher()
    w.start = lambda: None  # type: ignore[method-assign]
    assert "error" in w.add(1, {"task_id": 36})  # no plan
    assert "error" in w.add(1, {"then": "x"})  # nothing to watch
    for i in range(MAX_ACTIVE):
        assert "watching" in w.add(1, {"task_id": i, "then": "x"})
    assert "error" in w.add(1, {"task_id": 99, "then": "x"})
    first = w.watches[0].id
    assert w.cancel(first) and not w.cancel(first)


def test_watches_survive_a_restart(rig: Rig) -> None:
    w = rig.watcher()
    task_watch(w, task=36)
    again = rig.watcher()
    assert [x.task_id for x in again.watches] == [36]
    assert "watching" in task_watch(again, task=37)
    assert {x.id for x in again.watches} == {1, 2}  # ids keep counting
