"""Vision's conversations and goal, kept in the local SQLite so a restart forgets nothing.

A goal that was running when the backend stopped comes back **paused**, never running: it
would otherwise start spending simulations against an objective nobody restated, perhaps
days later. The person resumes it with ``/goal resume``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from ..db.models import AgentGoalRow, AgentThread
from .goals import Goal

if TYPE_CHECKING:
    from ..db.sqlite import Database

log = structlog.get_logger(__name__)

GOAL_ROW = 1
RESTART_REASON = "The backend restarted while this goal was running. /goal resume to carry on."


async def load_threads(db: Database) -> dict[int, list[dict[str, Any]]]:
    async with db.session() as session:
        rows = (await session.execute(select(AgentThread))).scalars().all()
        return {row.id: list(row.messages or []) for row in rows}


async def save_thread(db: Database, thread_id: int, messages: list[dict[str, Any]]) -> None:
    async with db.session() as session:
        row = await session.get(AgentThread, thread_id)
        if row is None:
            session.add(AgentThread(id=thread_id, messages=messages))
        else:
            row.messages = messages


async def load_goal(db: Database) -> Goal | None:
    async with db.session() as session:
        row = await session.get(AgentGoalRow, GOAL_ROW)
        if row is None or not row.data:
            return None
        try:
            goal = Goal.from_record(dict(row.data))
        except TypeError:
            log.warning("agent.goal_unreadable", exc_info=True)
            return None
    if goal.running:
        goal.status = "paused"
        goal.note("paused", RESTART_REASON)
    return goal


async def save_goal(db: Database, goal: Goal | None) -> None:
    async with db.session() as session:
        row = await session.get(AgentGoalRow, GOAL_ROW)
        data = goal.to_record() if goal is not None else None
        if row is None:
            session.add(AgentGoalRow(id=GOAL_ROW, data=data))
        else:
            row.data = data
