"""The research journal: what was tried, and what it cost to learn.

Findings live longer than conversations. A thread is gone on restart, so without this the
agent proposes the same sweep on a family that was closed a week ago, and the only record of
why it was closed is in someone's head.

Writing is deliberately cheap (no approval, nothing spent) because a memory that is
expensive to write does not get written.
"""

from __future__ import annotations

from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select

from ..db.models import ResearchNote
from ..schemas import Out
from .deps import State

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/journal", tags=["journal"])

Kind = Literal["finding", "dead_end", "decision", "idea"]


class NoteRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4_000, description="What was learned, plainly")
    kind: Kind = Field(default="finding", description="finding | dead_end | decision | idea")
    subject: str = Field(default="", max_length=128, description="Dataset, alpha, family or scope")
    scope: str = Field(default="", max_length=48, description="e.g. USA/1/TOP3000")
    tags: list[str] = Field(default_factory=list)
    author: Literal["vision", "human"] = "vision"


class Note(Out):
    id: int
    kind: str
    subject: str
    scope: str
    body: str
    tags: list[str]
    author: str
    created_at: str


def _out(row: ResearchNote) -> Note:
    return Note(
        id=row.id,
        kind=row.kind,
        subject=row.subject,
        scope=row.scope,
        body=row.body,
        tags=[str(t) for t in (row.tags or [])],
        author=row.author,
        created_at=row.created_at.isoformat(),
    )


@router.post("")
async def write(payload: NoteRequest, state: State) -> Note:
    """Record one thing learned. Local only, spends nothing."""
    row = ResearchNote(
        kind=payload.kind,
        subject=payload.subject.strip(),
        scope=payload.scope.strip(),
        body=payload.body.strip(),
        tags=payload.tags,
        author=payload.author,
    )
    async with state.db.session() as session:
        session.add(row)
        await session.flush()
        note = _out(row)
    log.info("journal.written", kind=note.kind, subject=note.subject)
    return note


@router.get("")
async def search(
    state: State,
    q: Annotated[str, Query(description="Matches body, subject or scope")] = "",
    kind: Annotated[Kind | None, Query()] = None,
    subject: Annotated[str, Query()] = "",
    limit: Annotated[int, Query(ge=1, le=200)] = 40,
) -> list[Note]:
    """Recent notes, newest first. Search before proposing a sweep."""
    stmt = select(ResearchNote).order_by(ResearchNote.created_at.desc()).limit(limit)
    if kind:
        stmt = stmt.where(ResearchNote.kind == kind)
    if subject:
        stmt = stmt.where(ResearchNote.subject.ilike(f"%{subject}%"))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                ResearchNote.body.ilike(like),
                ResearchNote.subject.ilike(like),
                ResearchNote.scope.ilike(like),
            )
        )
    async with state.db.session() as session:
        rows = (await session.execute(stmt)).scalars().all()
        return [_out(r) for r in rows]


@router.delete("/{note_id}", status_code=204)
async def remove(note_id: int, state: State) -> None:
    """Delete a note that turned out to be wrong. A wrong memory is worse than none."""
    async with state.db.session() as session:
        await session.execute(delete(ResearchNote).where(ResearchNote.id == note_id))
