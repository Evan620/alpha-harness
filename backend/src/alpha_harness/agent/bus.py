"""What Vision is doing, as an ordered log any number of panels can follow.

A turn the goal runner starts has no browser request to stream into: the person may have
closed the tab an hour ago. So every event it produces is published here with a sequence
number, and a panel follows the log from wherever it last read. Reopening the tab replays
what happened while it was away, then carries on live.

In memory and bounded on purpose: this is the live view, not the record. The conversation
itself is persisted separately (see :mod:`store`).
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

KEEP = 4_000
HEARTBEAT_SECONDS = 15.0


class EventBus:
    def __init__(self) -> None:
        self._log: deque[dict[str, Any]] = deque(maxlen=KEEP)
        self._seq = 0
        self._changed = asyncio.Event()
        #: Where the current goal's story begins, so a reopened panel replays that and not
        #: every earlier goal's lines.
        self.goal_from = 0

    def mark_goal_start(self) -> None:
        self.goal_from = self._seq

    @property
    def seq(self) -> int:
        return self._seq

    def publish(self, event: dict[str, Any]) -> None:
        self._seq += 1
        self._log.append({**event, "seq": self._seq})
        # Wake every follower, then give later ones a fresh event to wait on.
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def follow(self, since: int = 0) -> AsyncIterator[dict[str, Any]]:
        """Everything after ``since``, then new events as they happen. A heartbeat keeps an
        idle connection from being closed by a proxy."""
        while True:
            changed = self._changed  # taken before reading, so nothing slips between
            fresh = [e for e in self._log if e["seq"] > since]
            for event in fresh:
                since = event["seq"]
                yield event
            if fresh:
                continue
            try:
                await asyncio.wait_for(changed.wait(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                yield {"type": "ping", "seq": since}
