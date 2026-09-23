"""Vision's permission mode, chosen by the person in the UI, like Claude Code's modes.

- ``ask``: anything that writes, simulates or spends waits for an Approve click (default).
- ``auto``: the person has chosen to let Vision act without asking. Each action still goes
  through :class:`ApprovalGate` (proposal, payload hash, single execution, audit log).

Human-only actions (sign-in, keys, update, quit) are refused in every mode, and BRAIN
submission is blocked in ``BrainClient`` whatever the mode. The mode is set only through
``PUT /api/agent/permissions``, which the action catalog excludes, so Vision cannot change it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

import structlog

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)

Mode = Literal["ask", "auto"]
MODES: tuple[Mode, ...] = ("ask", "auto")


class Permissions:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._mode: Mode = "ask"
        try:
            stored = json.loads(path.read_text()).get("mode")
            if stored in MODES:
                self._mode = stored
        except OSError, ValueError:
            pass

    @property
    def mode(self) -> Mode:
        return self._mode

    def set(self, mode: Mode) -> Mode:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self._mode = mode
        try:
            self._path.write_text(json.dumps({"mode": mode}))
        except OSError:
            log.warning("vision.permissions_not_saved", exc_info=True)
        log.info("vision.permissions_changed", mode=mode)
        return mode
