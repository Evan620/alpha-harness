"""The account's correlation queue, as anyone in the app can see it.

BRAIN runs one correlation job per account, so this answers the question every slow read
raises: what is BRAIN working on, what is waiting behind it, and is the slot being held by
something outside this app, such as a submit started in the BRAIN web UI.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import RootModel

from .deps import State

router = APIRouter(prefix="/api/correlations", tags=["correlations"])


class QueueStatus(RootModel[dict[str, Any]]):
    """``running``, ``queued``, ``recent``, and ``held`` with a ``message`` when it is."""


@router.get("/queue")
async def queue_status(state: State) -> QueueStatus:
    """What the account's single correlation slot is doing right now. Reads nothing from BRAIN.

    ``held`` turns true once the running job has been pending longer than
    ``heldAfterSeconds``: the slot is probably taken by a submit in the BRAIN web UI or by
    another session, and every self, prod and check read waits behind it. ``recent`` keeps
    the last finished jobs with their outcome, ``done``, ``timeout``, ``error`` or
    ``cancelled``, so a timeout is never mistaken for a measurement.
    """
    return QueueStatus(state.correlations.status())


@router.post("/queue/clear")
async def clear_queue(state: State) -> QueueStatus:
    """Drop every queued correlation job. The one BRAIN is working on keeps running.

    For when a held slot has stacked up reads nobody needs any more. BRAIN cannot be told
    to abandon a job, so the running one is left to finish or time out.
    """
    dropped = state.correlations.clear()
    return QueueStatus(state.correlations.status() | {"dropped": dropped})
