"""Learning after the fact: review what just happened and keep what would change a decision.

Modelled on Hermes' background review and curator. The agent is not trusted to remember to
take notes in the middle of a task, so after a turn that did real work, and again when a goal
ends, a separate call re-reads the transcript and writes down:

- **notes** in the journal (findings, dead ends, decisions), marked ``vision-review``;
- a **playbook** when a procedure worked, or its pitfalls when one failed;
- at most one **rule proposal**, which steers nothing until the person accepts it.

It runs in the background, never holds the turn lock, and a failure only costs the lesson.
The **curator** runs after a goal ends, at most every few hours: it archives duplicate notes
and playbooks that keep failing. It archives; it never deletes.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from ..api.learning import add_proposal, upsert_playbook
from ..db.models import Playbook, ResearchNote
from .judge import _transcript

if TYPE_CHECKING:
    from .goals import Goal
    from .loop import AgentService, Thread

log = structlog.get_logger(__name__)

CURATOR_EVERY_SECONDS = 6 * 3600
MAX_NOTES = 3

REVIEW_SYSTEM = """\
You review the work of a research agent on WorldQuant BRAIN, after the fact, to keep what
would change a FUTURE decision. You see the transcript and the notes already kept.

Keep only what is new, specific and backed by the transcript: a number a tool returned, an
alpha id, a check result, an error and its cause. Never invent. If nothing is worth keeping,
return empty lists. Do not repeat a note that already exists in substance.

- notes: up to 3. kind is finding, dead_end (tried and failed, so do not retry), decision,
  or idea. subject is a dataset id, alpha id, family or scope. scope like USA/1/TOP3000.
- playbook: only when a multi-step procedure clearly WORKED (or, if the goal failed, to
  record the pitfalls of one that did not). name is short and reusable. steps are numbered
  and name the actions used. Otherwise null.
- rule: only when the evidence contradicts or sharpens a general rule for the whole
  platform, e.g. a check that behaves unlike its documentation. Otherwise null. Rare.

Reply with ONE JSON object and nothing else:
{"notes": [{"kind": "...", "subject": "...", "scope": "...", "body": "..."}],
 "playbook": null or {"name": "...", "when_to_use": "...", "steps": "...", "pitfalls": "..."},
 "rule": null or {"text": "...", "evidence": "..."}}
"""

CURATOR_SYSTEM = """\
You maintain a research agent's memory. Given its journal notes and playbooks, choose what to
ARCHIVE (archiving is recoverable; nothing is deleted):
- notes that are duplicates of a newer note, or superseded by one that says otherwise;
- playbooks with failures at least two more than successes, or duplicating a better one.
Keep anything unique. When unsure, keep it.

Reply with ONE JSON object: {"archive_notes": [ids], "archive_playbooks": [ids], "why": "..."}
"""


def _json(raw: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class Reflector:
    def __init__(self, service: AgentService) -> None:
        self.service = service
        self._tasks: set[asyncio.Task[None]] = set()
        self._last_curated = 0.0

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- triggers ------------------------------------------------------------

    def after_turn(self, thread: Thread) -> None:
        """Review a turn that did something. A turn with no action has nothing to learn."""
        if thread.last_actions:
            self._spawn(self._review(thread, outcome=None))

    def after_goal(self, goal: Goal, thread: Thread, used: list[int]) -> None:
        """A goal ended: learn from the whole run, credit the playbooks it used, and let the
        curator tidy up if it has not recently."""
        self._spawn(self._review(thread, outcome=goal))
        self._spawn(self._credit(used, success=goal.status == "met"))
        if time.time() - self._last_curated > CURATOR_EVERY_SECONDS:
            self._last_curated = time.time()
            self._spawn(self._curate())

    # -- the review ---------------------------------------------------------

    async def _recent_notes(self, limit: int = 30) -> list[ResearchNote]:
        async with self.service.state.db.session() as session:
            stmt = (
                select(ResearchNote)
                .where(ResearchNote.archived.is_(False))
                .order_by(ResearchNote.created_at.desc())
                .limit(limit)
            )
            return list((await session.execute(stmt)).scalars().all())

    async def _review(self, thread: Thread, outcome: Goal | None) -> None:
        try:
            notes = await self._recent_notes()
            known = "\n".join(f"- [{n.kind}] {n.subject}: {n.body[:160]}" for n in notes) or "none"
            head = (
                f"The goal '{outcome.objective}' ended: {outcome.status}. "
                f"{outcome.stopped_reason or outcome.last_reason}\n\n"
                if outcome is not None
                else "One turn just finished.\n\n"
            )
            user = f"{head}Notes already kept:\n{known}\n\nTranscript:\n{_transcript(thread)}"
            answer = await self.service.state.llm.generate(
                system=REVIEW_SYSTEM, user=user, temperature=0.0
            )
            data = _json(answer.text)
            if data is None:
                log.info("reflect.unreadable")
                return
            await self._keep(data)
        except Exception:
            log.warning("reflect.failed", exc_info=True)

    async def _keep(self, data: dict[str, Any]) -> None:
        kept = []
        async with self.service.state.db.session() as session:
            for note in (data.get("notes") or [])[:MAX_NOTES]:
                if not isinstance(note, dict) or not str(note.get("body") or "").strip():
                    continue
                kind = str(note.get("kind") or "finding")
                if kind not in {"finding", "dead_end", "decision", "idea"}:
                    kind = "finding"
                row = ResearchNote(
                    kind=kind,
                    subject=str(note.get("subject") or "")[:128],
                    scope=str(note.get("scope") or "")[:48],
                    body=str(note["body"]).strip()[:4_000],
                    author="vision-review",
                    tags=["review"],
                )
                session.add(row)
                kept.append(row.subject or row.kind)
        playbook = data.get("playbook")
        if isinstance(playbook, dict) and str(playbook.get("name") or "").strip():
            await upsert_playbook(self.service.state, playbook, author="vision-review")
            kept.append(f"playbook '{playbook['name']}'")
        rule = data.get("rule")
        if isinstance(rule, dict) and str(rule.get("text") or "").strip():
            proposal = await add_proposal(
                self.service.state,
                str(rule["text"]),
                str(rule.get("evidence") or ""),
                author="vision-review",
            )
            self.service.bus.publish(
                {
                    "type": "loop",
                    "kind": "status",
                    "text": f"Vision proposes a rule (#{proposal.id}): {proposal.text} "
                    f"Type /rules accept {proposal.id} or /rules reject {proposal.id}.",
                }
            )
        if kept:
            log.info("reflect.kept", items=kept)
            self.service.bus.publish(
                {"type": "loop", "kind": "status", "text": "Learned: " + "; ".join(kept[:5])}
            )

    async def _credit(self, used: list[int], *, success: bool) -> None:
        if not used:
            return
        async with self.service.state.db.session() as session:
            for pid in used:
                row = await session.get(Playbook, pid)
                if row is None:
                    continue
                if success:
                    row.successes += 1
                else:
                    row.failures += 1

    # -- the curator ----------------------------------------------------------

    async def _curate(self) -> None:
        try:
            notes = await self._recent_notes(limit=80)
            async with self.service.state.db.session() as session:
                books = list(
                    (await session.execute(select(Playbook).where(Playbook.status == "active")))
                    .scalars()
                    .all()
                )
            if len(notes) < 10 and not books:
                return
            listing = (
                "\n".join(
                    f"note {n.id} [{n.kind}] {n.subject} ({n.created_at:%Y-%m-%d}): {n.body[:140]}"
                    for n in notes
                )
                + "\n"
                + "\n".join(
                    f"playbook {b.id} '{b.name}': uses {b.uses}, successes {b.successes}, "
                    f"failures {b.failures}. {b.when_to_use[:120]}"
                    for b in books
                )
            )
            answer = await self.service.state.llm.generate(
                system=CURATOR_SYSTEM, user=listing, temperature=0.0
            )
            data = _json(answer.text) or {}
            note_ids = {int(i) for i in data.get("archive_notes") or [] if str(i).isdigit()}
            book_ids = {int(i) for i in data.get("archive_playbooks") or [] if str(i).isdigit()}
            async with self.service.state.db.session() as session:
                for n in notes:
                    if n.id in note_ids:
                        row = await session.get(ResearchNote, n.id)
                        if row is not None:
                            row.archived = True
                for b in books:
                    if b.id in book_ids:
                        row = await session.get(Playbook, b.id)
                        if row is not None:
                            row.status = "archived"
            if note_ids or book_ids:
                log.info("curator.archived", notes=len(note_ids), playbooks=len(book_ids))
        except Exception:
            log.warning("curator.failed", exc_info=True)


async def playbooks_for(service: AgentService, text: str, limit: int = 2) -> list[Playbook]:
    """The active playbooks most relevant to what is being asked, by word overlap. Plain on
    purpose: a wrong match costs a paragraph of prompt, a fancy one costs a model call."""
    words = set(re.findall(r"[a-z0-9]{3,}", text.lower()))
    if not words:
        return []
    async with service.state.db.session() as session:
        books = list(
            (await session.execute(select(Playbook).where(Playbook.status == "active")))
            .scalars()
            .all()
        )
    scored = []
    for b in books:
        hay = set(re.findall(r"[a-z0-9]{3,}", f"{b.name} {b.when_to_use}".lower()))
        overlap = len(words & hay)
        if overlap >= 2:
            scored.append((overlap + b.successes - b.failures, b))
    scored.sort(key=lambda s: -s[0])
    return [b for _, b in scored[:limit]]
