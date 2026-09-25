"""Read-only SQL over what this machine already knows.

The agent can call every screen in the app, but a screen answers the question it was built
for. "Which of these near-misses fails only one check, and by how little" is arithmetic over
thousands of rows, and reasoning about it from paged JSON is how a model invents numbers.

Two stores are exposed, both read-only and both local: ``catalog`` (DuckDB: data fields,
datasets, the Alpha vault) and ``history`` (SQLite: simulations, studies, trials). Nothing
here reaches BRAIN, so it spends no quota and answers in milliseconds.

**The guard is the point.** DuckDB and SQLite would both happily run a DELETE through a
connection named "query", so the statement is checked before it is sent rather than trusted:
one statement, and it must be a read.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..schemas import Out
from .deps import State

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/analyse", tags=["analyse"])

MAX_ROWS = 500

#: Only these may begin a statement. Everything that writes, attaches, copies or loads is
#: absent by construction rather than blacklisted, so a verb nobody thought of is refused.
READ_VERBS = ("select", "with", "describe", "show", "explain", "summarize", "pragma table_info")

#: Belt and braces for a read verb hiding a write, e.g. a CTE containing DELETE, or DuckDB's
#: COPY ... TO, which writes a file from a SELECT. REPLACE is deliberately absent: it is an
#: everyday string function in reads, and CREATE OR REPLACE is already caught by CREATE.
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|export|import|install|"
    r"load|vacuum|checkpoint|truncate|grant|revoke|call|set|reset)\b",
    re.IGNORECASE,
)

_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


class SqlRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=8_000, description="One read-only statement")
    source: Literal["catalog", "history"] = Field(
        default="catalog",
        description="catalog = data fields, datasets, Alpha vault (DuckDB); "
        "history = simulations, studies, trials (SQLite)",
    )
    limit: int = Field(default=200, ge=1, le=MAX_ROWS)


class SqlResult(Out):
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    source: str


class SqlRefused(ValueError):
    """The statement is not a single read. Never sent to a database."""


def guard(sql: str) -> str:
    """Return the statement, or raise :class:`SqlRefused`.

    Comments are stripped before the check so ``/* */`` cannot hide a verb, and a trailing
    semicolon is allowed but a second statement is not.
    """
    bare = _COMMENT.sub(" ", sql).strip().rstrip(";").strip()
    if not bare:
        raise SqlRefused("Empty statement.")
    if ";" in bare:
        raise SqlRefused("One statement at a time.")
    lowered = bare.lower()
    if not lowered.startswith(READ_VERBS):
        raise SqlRefused(f"Only reads are allowed here: start with one of {', '.join(READ_VERBS)}.")
    found = FORBIDDEN.search(bare)
    if found:
        raise SqlRefused(f"{found.group(0).upper()} is not allowed: this endpoint only reads.")
    return bare


@router.get("/schema")
async def schema(
    state: State,
    source: Annotated[Literal["catalog", "history"], Query()] = "catalog",
) -> dict[str, Any]:
    """Every table and column in one store, so a query can be written without guessing."""
    if source == "catalog":
        rows = await state.catalog.query(
            "select table_name, column_name, data_type from information_schema.columns "
            "where table_schema = 'main' order by table_name, ordinal_position"
        )
        tables: dict[str, list[str]] = {}
        for row in rows:
            tables.setdefault(str(row["table_name"]), []).append(
                f"{row['column_name']} {row['data_type']}"
            )
        return {"source": source, "tables": tables}

    async with state.db.engine.connect() as conn:
        names = [
            r[0]
            for r in (
                await conn.execute(
                    text("select name from sqlite_master where type='table' order by name")
                )
            ).all()
        ]
        out: dict[str, list[str]] = {}
        for name in names:
            cols = (await conn.execute(text(f'pragma table_info("{name}")'))).all()
            out[name] = [f"{c[1]} {c[2]}" for c in cols]
    return {"source": source, "tables": out}


@router.post("/sql")
async def run_sql(payload: SqlRequest, state: State) -> SqlResult:
    """Run one read-only statement against the local store and return its rows.

    Spends nothing: no BRAIN call, no simulation, no LLM budget.
    """
    try:
        statement = guard(payload.sql)
    except SqlRefused as refused:
        # The caller has to know WHY, or it cannot write a statement that passes.
        raise HTTPException(
            status_code=400, detail={"code": "sql_refused", "message": str(refused)}
        ) from refused
    # Supplied SQL is the feature. `statement` has passed `guard` (one read, no write verbs)
    # and `limit` is an int Pydantic has already bounded, so there is nothing to parameterise.
    wrapped = f"select * from ({statement}) limit {payload.limit + 1}"  # noqa: S608

    try:
        if payload.source == "catalog":
            rows = await state.catalog.query(wrapped)
        else:
            async with state.db.engine.connect() as conn:
                result = await conn.execute(text(wrapped))
                keys = list(result.keys())
                rows = [dict(zip(keys, r, strict=True)) for r in result.all()]
    except Exception as failed:  # a bad column name is the caller's to fix, not a crash
        log.info("analyse.sql_failed", source=payload.source, error=str(failed))
        raise HTTPException(
            status_code=400,
            detail={"code": "sql_failed", "message": str(failed)[:400]},
        ) from failed

    truncated = len(rows) > payload.limit
    rows = rows[: payload.limit]
    return SqlResult(
        columns=list(rows[0]) if rows else [],
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        source=payload.source,
    )
