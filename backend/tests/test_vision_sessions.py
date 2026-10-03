"""/sessions and /resume: Vision's saved conversations listed and rebuilt as panel entries.

The service is built bare (no app, no BRAIN): these methods read only the threads, the goal
and the action index, so the test pins their output without starting the harness.
"""

from __future__ import annotations

import json
from typing import Any

from alpha_harness.agent.goals import Goal, GoalBook
from alpha_harness.agent.loop import AgentService, Thread


class _Runner:
    running = True


def bare(threads: list[Thread], goal: Goal | None = None) -> AgentService:
    service = object.__new__(AgentService)
    service.threads = {t.id: t for t in threads}
    service.goals = GoalBook()
    if goal is not None:
        service.goals.set(goal)
    service.runner = _Runner()  # type: ignore[assignment]
    service.index = {"get_research_state": {"method": "GET", "path": "/api/research/state"}}
    return service


def call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


def test_sessions_are_newest_first_and_mark_the_goal_thread() -> None:
    asked = Thread(id=1, messages=[{"role": "user", "content": "what is turnover?"}], updated=10)
    looped = Thread(
        id=2,
        messages=[{"role": "user", "content": "[Goal set by the person]\nGoal: find one\n"}],
        updated=20,
    )
    empty = Thread(id=3, messages=[], updated=30)
    goal = Goal(objective="find one", thread_id=2)
    rows = bare([asked, looped, empty], goal).sessions()
    assert [r["id"] for r in rows] == [2, 1]  # newest first, empty threads skipped
    assert rows[0]["title"] == "find one"
    assert rows[0]["goal"] == {"objective": "find one", "status": "active"}
    assert rows[0]["running"] is True
    assert rows[1]["title"] == "what is turnover?"
    assert rows[1]["goal"] is None


def test_history_rebuilds_entries_with_live_labels_and_results() -> None:
    thread = Thread(
        id=7,
        messages=[
            {"role": "user", "content": "how is my research going?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    call("c1", "call_action", {"name": "get_research_state"}),
                    call("c2", "find_actions", {"query": "journal"}),
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"loop": "search"})},
            {"role": "tool", "tool_call_id": "c2", "content": json.dumps({"error": "boom"})},
            {"role": "assistant", "content": "You are in the search loop."},
            {"role": "user", "content": "[Continuing toward your goal, turn 2 of 6]\nGoal: x"},
        ],
    )
    entries = bare([thread]).history(7)
    assert entries is not None
    assert entries[0] == {"role": "user", "text": "how is my research going?"}
    agent = entries[1]
    assert agent["role"] == "agent" and agent["live"] is False
    tools = [p for p in agent["parts"] if p["kind"] == "tool"]
    assert tools[0]["label"] == "GET /api/research/state"
    assert tools[0]["status"] == "executed"
    assert tools[1]["label"] == "search: journal"
    assert tools[1]["status"] == "error"
    # Both completions of one turn land in one agent entry, text after the tools.
    assert agent["parts"][-1] == {"kind": "text", "text": "You are in the search loop."}
    assert entries[2] == {
        "role": "loop",
        "kind": "continue",
        "text": "Continuing toward your goal, turn 2 of 6",
    }


def test_history_of_an_unknown_session_is_none() -> None:
    assert bare([]).history(99) is None


def test_a_finished_goal_still_reflects_on_its_own_thread() -> None:
    from alpha_harness.agent.runner import GoalRunner

    class Bus:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def publish(self, event: dict[str, Any]) -> None:
            self.events.append(event)

    class Reflector:
        def __init__(self) -> None:
            self.calls: list[tuple[Goal, Thread]] = []

        def after_goal(self, goal: Goal, thread: Thread, _used: list[int]) -> None:
            self.calls.append((goal, thread))

    thread = Thread(id=4, messages=[{"role": "user", "content": "go"}])
    goal = Goal(objective="find one", thread_id=4)
    service = bare([thread], goal)
    service.bus = Bus()  # type: ignore[assignment]
    service.reflector = Reflector()  # type: ignore[assignment]
    service.used_playbooks = {}
    goal.end("met", "found it")
    assert service.goals.goal is None  # cleared itself
    runner = object.__new__(GoalRunner)
    runner.service = service
    runner._announce({"action": "stop", "goal": goal.to_dict()})
    assert service.reflector.calls == [(goal, thread)]  # type: ignore[attr-defined]
    assert service.bus.events[-1]["text"].startswith("Met: found it")  # type: ignore[attr-defined]
