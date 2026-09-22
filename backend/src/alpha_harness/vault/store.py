"""Keeping every alpha and its daily returns.

An alpha is a permanent result, so a local copy means never spending quota on it twice.

The daily returns matter more: two alphas with mediocre Sharpe that move independently
combine into something better than either, and which pairs those are is only answerable
if the series are kept. The stored series rebuilds the platform's own figures (see
:mod:`.metrics`), so a mix can be judged before a simulation is spent on it.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import structlog

from ..db.duck import ALPHA_COLUMNS, TRAIN_COLUMNS, Catalog
from . import metrics

if TYPE_CHECKING:
    from ..brain.schemas import Alpha

log = structlog.get_logger(__name__)

#: 3: turnover calibrated to ``yearly-stats`` where it misses by more than rounding. A series
#: stored under an older version is fetched again on the next sync.
SERIES_VERSION = 3


def checks_json(alpha: Alpha) -> str | None:
    """An alpha's submission checks as the string the vault stores.

    Shared with the backfill, which reads the same array to decide whether the platform
    is worth asking to finish checking an alpha.
    """
    stats = alpha.in_sample
    if stats is None:
        return None
    return json.dumps([c.model_dump(by_alias=True) for c in stats.checks])


def _names(alpha: Alpha, key: str) -> list[str]:
    """The ``name`` of each entry in one of the Alpha's undeclared lists, e.g. its
    classifications ("Power Pool Alpha") or pyramids ("ASI/D1/OTHER")."""
    raw = (alpha.model_extra or {}).get(key) or []
    return [str(c["name"]) for c in raw if isinstance(c, dict) and c.get("name")]


def _end_date(alpha: Alpha) -> date | None:
    raw = (alpha.settings.model_extra or {}).get("endDate") if alpha.settings else None
    return _as_date(raw) if raw else None


def alpha_row(alpha: Alpha, fetched_at: datetime) -> tuple[Any, ...]:
    """One alpha flattened for storage."""
    stats = alpha.in_sample
    settings = alpha.settings
    code = alpha.regular or alpha.combo or alpha.selection
    return (
        alpha.id,
        (code.code if code else None),
        str(alpha.type) if alpha.type else None,
        settings.instrument_type if settings else None,
        settings.region if settings else None,
        settings.delay if settings else None,
        settings.universe if settings else None,
        settings.neutralization if settings else None,
        settings.decay if settings else None,
        settings.truncation if settings else None,
        stats.sharpe if stats else None,
        stats.fitness if stats else None,
        stats.turnover if stats else None,
        stats.returns if stats else None,
        stats.drawdown if stats else None,
        stats.margin if stats else None,
        stats.long_count if stats else None,
        stats.short_count if stats else None,
        alpha.grade,
        alpha.stage,
        alpha.status,
        (code.operator_count if code else None),
        alpha.date_created,
        checks_json(alpha),
        fetched_at,
        alpha.name,
        alpha.date_submitted,
        settings.max_trade if settings else None,
        settings.max_position if settings else None,
        json.dumps(alpha.tags),
        json.dumps(_names(alpha, "classifications")),
        json.dumps(_names(alpha, "pyramids")),
        stats.pnl if stats else None,
        _end_date(alpha),
        settings.simulation_mode if settings else None,
    )


#: What the Simulations table may sort and filter on, as SQL over ``alpha a``. Anything
#: not in here is ignored rather than interpolated.
ALPHA_METRICS: dict[str, str] = {
    "sharpe": "a.sharpe",
    "fitness": "a.fitness",
    "turnover": "a.turnover",
    "returns": "a.returns",
    "drawdown": "a.drawdown",
    "margin": "a.margin",
    "operator_count": "a.operator_count",
    "calmar": "CASE WHEN a.drawdown > 0 THEN a.returns / a.drawdown END",
    "date_created": "a.date_created",
    "date_submitted": "a.date_submitted",
}

#: Anything past UNSUBMITTED (ACTIVE, DECOMMISSIONED, ...) has been submitted.
SUBMITTED = "(a.status IS NOT NULL AND a.status <> 'UNSUBMITTED')"

#: Types that can seed the Evolution Lab.
#:
#: An ``RA_CHILD`` is an ordinary Alpha that happens to have arrived through a
#: region-agnostic run: it carries one real region, that region's own universe, its own
#: Sharpe and Fitness, and an expression. Excluding it left anyone running region-agnostic
#: simulations with a vault full of Alphas that Evolution refused to breed from.
#:
#: The ``RA_PARENT`` is the one to keep out, and not because of its type — it is a summary
#: of its children with region ``ALL`` and no metrics of its own (measured: Sharpe and
#: Fitness both null), so there is nothing to score it by and no single market to breed in.
EVOLVABLE = "(coalesce(a.sim_type, 'REGULAR') IN ('REGULAR', 'RA_CHILD'))"

#: The same rule, for a row already in hand.
EVOLVABLE_TYPES = frozenset({"REGULAR", "RA_CHILD"})


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _page_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "alphaId": r["alpha_id"],
        "name": r["name"],
        "type": r["sim_type"],
        "status": r["status"],
        "region": r["region"],
        "universe": r["universe"],
        "delay": r["delay"],
        "neutralization": r["neutralization"],
        "decay": r["decay"],
        "truncation": r["truncation"],
        "expression": r["expression"],
        "sharpe": r["sharpe"],
        "fitness": r["fitness"],
        "turnover": r["turnover"],
        "returns": r["returns"],
        "drawdown": r["drawdown"],
        "margin": r["margin"],
        "operatorCount": r["operator_count"],
        "calmar": r["calmar"],
        "dateCreated": _iso(r["date_created"]),
        "dateSubmitted": _iso(r["date_submitted"]),
        "hasPnl": bool(r["has_pnl"]),
        "longCount": r["long_count"],
        "shortCount": r["short_count"],
        "maxTrade": r["max_trade"],
        "maxPosition": r["max_position"],
        "classifications": _json_list(r["classifications"]),
        "pyramids": _json_list(r["pyramids"]),
        "trainSharpe": r["train_sharpe"],
        "testSharpe": r["test_sharpe"],
    }


def _json_list(raw: Any) -> list[str]:
    return [str(v) for v in json.loads(raw)] if isinstance(raw, str) else []


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class AlphaVault:
    """The local record of every alpha and its returns."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    # -- writing ---------------------------------------------------------

    async def save_alpha(self, alpha: Alpha, *, fetched_at: datetime | None = None) -> None:
        from ..db.models import utcnow

        await self._save([alpha], fetched_at or utcnow())

    async def save_alphas(self, alphas: list[Alpha]) -> int:
        """A page of alphas in one write. Ids must be distinct within the page."""
        from ..db.models import utcnow

        return await self._save(alphas, utcnow())

    async def _save(self, alphas: list[Alpha], fetched_at: datetime) -> int:
        """Store alphas, carrying train and test statistics only for the ones that have
        them, so a listing that leaves them out never blanks stored values.

        Refused when no Alpha carries metrics or settings: every stored Alpha has both, so
        that is BRAIN's format changing, and writing it would blank the stored rows.
        """
        if alphas and all(a.in_sample is None and a.settings is None for a in alphas):
            raise ValueError(
                f"BRAIN returned {len(alphas)} Alpha(s) without metrics or settings, "
                "so nothing was stored. The platform's Alpha format may have changed."
            )
        written = await self.catalog.upsert_alphas(
            [alpha_row(a, fetched_at) for a in alphas if a.train is None]
        )
        return written + await self.catalog.upsert(
            "alpha",
            ALPHA_COLUMNS + TRAIN_COLUMNS,
            [
                (
                    *alpha_row(a, fetched_at),
                    a.train.sharpe,
                    a.train.fitness,
                    a.test.sharpe if a.test else None,
                    a.test.fitness if a.test else None,
                    _as_date(a.test.start_date) if a.test and a.test.start_date else None,
                    a.test.turnover if a.test else None,
                )
                for a in alphas
                if a.train
            ],
        )

    async def save_pnl(
        self,
        alpha_id: str,
        pnl_rows: list[dict[str, Any]],
        turnover_rows: list[dict[str, Any]],
        yearly_rows: list[dict[str, Any]],
    ) -> int:
        """Store one alpha's daily PnL and turnover from its recordsets, the turnover scaled to
        BRAIN's yearly figures (see :func:`metrics.calibrate`)."""
        days = metrics.daily_rows(pnl_rows, turnover_rows)
        stored = await self.by_ids([alpha_id])
        split = (stored.get(alpha_id) or {}).get("test_start")
        if yearly_rows:
            days = metrics.calibrate(days, yearly_rows, split if isinstance(split, date) else None)
        written = await self.catalog.replace_pnl(
            alpha_id, [(alpha_id, day, pnl, turnover) for day, pnl, turnover in days]
        )
        if written:
            await self.catalog.upsert(
                "alpha", ("alpha_id", "series_version"), [(alpha_id, SERIES_VERSION)]
            )
        return written

    async def save_checks(self, alpha_id: str, checks: list[dict[str, Any]]) -> None:
        """Replace one alpha's check array and touch nothing else.

        A partial-column upsert: the platform's ``/check`` endpoint returns resolved
        checks and no metrics, so writing a whole row from it would blank out the Sharpe
        this alpha was stored with.
        """
        await self.catalog.upsert("alpha", ("alpha_id", "checks"), [(alpha_id, json.dumps(checks))])

    # -- reading ---------------------------------------------------------

    async def counts(self) -> dict[str, Any]:
        rows = await self.catalog.query(
            """
            SELECT
                (SELECT count(*) FROM alpha)                              AS alphas,
                (SELECT count(DISTINCT alpha_id) FROM alpha_pnl)          AS with_returns,
                (SELECT count(*) FROM alpha_pnl)                          AS daily_rows,
                (SELECT count(*) FROM alpha WHERE sharpe IS NOT NULL)     AS scored
            """
        )
        row = rows[0] if rows else {}
        return {
            "alphas": int(row.get("alphas") or 0),
            "withReturns": int(row.get("with_returns") or 0),
            "dailyRows": int(row.get("daily_rows") or 0),
            "scored": int(row.get("scored") or 0),
            "missingReturns": int(row.get("alphas") or 0) - int(row.get("with_returns") or 0),
        }

    async def scopes(self) -> list[dict[str, Any]]:
        """Which scopes hold alphas. Mixing only ever happens inside one of these."""
        return await self.catalog.query(
            """
            SELECT instrument_type, region, delay, universe,
                   count(*) AS alphas,
                   count(*) FILTER (WHERE sharpe IS NOT NULL) AS scored,
                   max(sharpe) AS best_sharpe
            FROM alpha
            WHERE region IS NOT NULL
            GROUP BY 1, 2, 3, 4
            ORDER BY alphas DESC
            """
        )

    async def evolvable_markets(self) -> list[dict[str, Any]]:
        """Equity scopes holding unsubmitted alphas the Evolution Lab would take as seeds.

        Fitness included, as the lab requires it: a market counted without it advertises
        seeds that Auto Select then cannot use.
        """
        return await self.catalog.query(
            f"""
            SELECT a.region, a.delay, a.universe, count(*) AS alphas FROM alpha a
            WHERE coalesce(a.instrument_type, 'EQUITY') = 'EQUITY'
              AND a.region IS NOT NULL AND a.delay IS NOT NULL AND a.universe IS NOT NULL
              AND NOT {SUBMITTED} AND {EVOLVABLE}
              AND a.expression IS NOT NULL AND a.fitness IS NOT NULL
            GROUP BY 1, 2, 3
            ORDER BY alphas DESC
            """  # noqa: S608
        )

    async def stored_count(self) -> int:
        """Alphas stored from a BRAIN listing (they carry a creation date)."""
        value = await self.catalog.scalar(
            "SELECT count(*) FROM alpha WHERE date_created IS NOT NULL"
        )
        return int(value or 0)

    async def without_returns(self, limit: int = 5000) -> list[str]:
        """Alphas whose daily series has not been fetched yet, or only in the old form."""
        rows = await self.catalog.query(
            """
            SELECT a.alpha_id FROM alpha a
            WHERE a.series_version IS DISTINCT FROM ?
            ORDER BY a.sharpe DESC NULLS LAST
            LIMIT ?
            """,
            [SERIES_VERSION, limit],
        )
        return [str(r["alpha_id"]) for r in rows]

    async def latest_created(self) -> datetime | None:
        """The newest alpha stored, which is where an incremental sync resumes."""
        return await self.catalog.scalar("SELECT max(date_created) FROM alpha")

    async def page(
        self,
        *,
        submitted: bool = False,
        sort_by: str = "date_created",
        sort_desc: bool = True,
        regions: list[str] | None = None,
        delays: list[int] | None = None,
        universes: list[str] | None = None,
        minimum: dict[str, float] | None = None,
        maximum: dict[str, float] | None = None,
        search: str | None = None,
        evolvable: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """One page of the Simulations table: sorted, filtered, with the total.

        ``evolvable`` keeps only what the Evolution Lab can breed from. The same predicate
        the lab itself enforces, so the table offering an Alpha and the lab accepting it can
        never disagree — a SuperAlpha carries a combo expression rather than a regular one,
        and picking one only to be told later that it cannot be a seed is the table's fault,
        not the user's.
        """
        clauses = ["a.date_created IS NOT NULL", SUBMITTED if submitted else f"NOT {SUBMITTED}"]
        if evolvable:
            # Every condition ``ga.seed_problem`` and ``ga.auto_seeds`` apply, not just the
            # type: an Alpha with no Fitness is refused there too, so offering it here would
            # be the same lie in a different place.
            clauses.extend(
                [
                    EVOLVABLE,
                    "coalesce(a.instrument_type, 'EQUITY') = 'EQUITY'",
                    "a.expression IS NOT NULL",
                    "a.fitness IS NOT NULL",
                ]
            )
        params: list[Any] = []
        for column, values in (("region", regions), ("delay", delays), ("universe", universes)):
            if values:
                clauses.append(f"a.{column} IN ({', '.join('?' for _ in values)})")
                params.extend(values)
        for bounds, op in ((minimum, ">="), (maximum, "<=")):
            for key, value in (bounds or {}).items():
                if key in ALPHA_METRICS and not key.startswith("date_"):
                    clauses.append(f"({ALPHA_METRICS[key]}) {op} ?")
                    params.append(value)
        if search:
            needle = f"%{search.lower()}%"
            clauses.append(
                "(lower(a.alpha_id) LIKE ? OR lower(coalesce(a.expression, '')) LIKE ? "
                "OR lower(coalesce(a.name, '')) LIKE ?)"
            )
            params.extend([needle, needle, needle])

        where = " AND ".join(clauses)
        order = ALPHA_METRICS.get(sort_by, ALPHA_METRICS["date_created"])
        direction = "DESC" if sort_desc else "ASC"
        total = await self.catalog.scalar(f"SELECT count(*) FROM alpha a WHERE {where}", params)  # noqa: S608
        rows = await self.catalog.query(
            f"""
            SELECT a.alpha_id, a.name, a.sim_type, a.status, a.region, a.universe, a.delay,
                   a.neutralization, a.decay, a.truncation, a.expression, a.sharpe,
                   a.fitness, a.turnover, a.returns, a.drawdown, a.margin,
                   a.operator_count, {ALPHA_METRICS["calmar"]} AS calmar,
                   a.date_created, a.date_submitted, a.long_count, a.short_count,
                   a.max_trade, a.max_position, a.classifications, a.pyramids,
                   a.train_sharpe, a.test_sharpe,
                   EXISTS (SELECT 1 FROM alpha_pnl p WHERE p.alpha_id = a.alpha_id) AS has_pnl
            FROM alpha a
            WHERE {where}
            ORDER BY {order} {direction} NULLS LAST, a.alpha_id
            LIMIT ? OFFSET ?
            """,  # noqa: S608
            [*params, limit, offset],
        )
        return {"total": int(total or 0), "results": [_page_row(r) for r in rows]}

    async def pnl_series(self, alpha_id: str) -> list[dict[str, Any]]:
        """The stored daily PnL, oldest first, as ``{date, pnl}`` rows."""
        return await self.catalog.query(
            "SELECT date, pnl FROM alpha_pnl WHERE alpha_id = ? ORDER BY date", [alpha_id]
        )

    async def series_length(self, alpha_id: str) -> int:
        value = await self.catalog.scalar(
            "SELECT count(*) FROM alpha_pnl WHERE alpha_id = ?", [alpha_id]
        )
        return int(value or 0)

    async def daily_pnl(self, alpha_ids: list[str]) -> dict[str, dict[date, float]]:
        """Each Alpha's stored daily PnL by date, oldest first. Alphas without one are absent."""
        if not alpha_ids:
            return {}
        placeholders = ", ".join("?" for _ in alpha_ids)
        rows = await self.catalog.query(
            f"""
            SELECT alpha_id, date, pnl FROM alpha_pnl
            WHERE alpha_id IN ({placeholders})
            ORDER BY alpha_id, date
            """,  # noqa: S608
            list(alpha_ids),
        )
        grouped: dict[str, dict[date, float]] = {}
        for row in rows:
            grouped.setdefault(str(row["alpha_id"]), {})[row["date"]] = float(row["pnl"] or 0.0)
        return grouped

    async def series(self, alpha_ids: list[str]) -> dict[str, dict[date, tuple[float, float]]]:
        """Each Alpha's daily ``(pnl, turnover)`` by date. Alphas still on the old series,
        which has no turnover, are absent."""
        if not alpha_ids:
            return {}
        placeholders = ", ".join("?" for _ in alpha_ids)
        rows = await self.catalog.query(
            f"""
            SELECT alpha_id, date, pnl, turnover FROM alpha_pnl
            WHERE alpha_id IN ({placeholders}) AND turnover IS NOT NULL
            ORDER BY alpha_id, date
            """,  # noqa: S608
            list(alpha_ids),
        )
        grouped: dict[str, dict[date, tuple[float, float]]] = {}
        for row in rows:
            grouped.setdefault(str(row["alpha_id"]), {})[row["date"]] = (
                float(row["pnl"] or 0.0),
                float(row["turnover"]),
            )
        return grouped

    async def submitted_members(self) -> list[dict[str, Any]]:
        """Every submitted Alpha with what the Portfolio page filters on.

        The figures are BRAIN's own, as it reported them for the Alpha. Nothing here is
        recomputed: only the combination of several Alphas is ours to work out, because
        that is the one thing BRAIN does not publish.
        """
        return await self.catalog.query(
            f"""
            SELECT a.alpha_id, a.name, a.region, a.delay, a.universe, a.max_trade,
                   a.max_position, a.tags, a.classifications, a.pyramids, a.date_submitted,
                   a.sharpe, a.turnover, a.fitness, a.returns, a.drawdown, a.margin,
                   EXISTS (
                       SELECT 1 FROM alpha_pnl p
                       WHERE p.alpha_id = a.alpha_id AND p.turnover IS NOT NULL
                   ) AS has_series
            FROM alpha a
            WHERE {SUBMITTED}
            ORDER BY a.date_submitted DESC NULLS LAST, a.alpha_id
            """  # noqa: S608
        )

    async def lacking_series(self, alpha_ids: list[str]) -> list[str]:
        """Those of these Alphas with no PnL and turnover stored, in the order given."""
        if not alpha_ids:
            return []
        placeholders = ", ".join("?" for _ in alpha_ids)
        rows = await self.catalog.query(
            f"""
            SELECT alpha_id FROM alpha
            WHERE alpha_id IN ({placeholders}) AND series_version = ?
            """,  # noqa: S608
            [*alpha_ids, SERIES_VERSION],
        )
        stored = {str(r["alpha_id"]) for r in rows}
        return [a for a in alpha_ids if a not in stored]

    async def train_pnl(self, alpha_ids: list[str]) -> dict[str, dict[date, float]]:
        """Each Alpha's daily PnL before its last two years, which a train/test split holds out.

        Only Alphas with a stored series appear.
        """
        if not alpha_ids:
            return {}
        placeholders = ", ".join("?" for _ in alpha_ids)
        rows = await self.catalog.query(
            f"""
            SELECT p.alpha_id, p.date, p.pnl FROM alpha_pnl p
            JOIN (
                SELECT alpha_id, max(date) - INTERVAL 2 YEAR AS cutoff FROM alpha_pnl
                WHERE alpha_id IN ({placeholders}) GROUP BY alpha_id
            ) c ON p.alpha_id = c.alpha_id
            WHERE p.date < c.cutoff
            """,  # noqa: S608
            list(alpha_ids),
        )
        out: dict[str, dict[date, float]] = {}
        for row in rows:
            out.setdefault(str(row["alpha_id"]), {})[row["date"]] = float(row["pnl"] or 0.0)
        return out

    async def by_ids(self, alpha_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Look several alphas up at once, keyed by id."""
        if not alpha_ids:
            return {}
        placeholders = ", ".join("?" for _ in alpha_ids)
        rows = await self.catalog.query(
            f"SELECT * FROM alpha WHERE alpha_id IN ({placeholders})",  # noqa: S608
            list(alpha_ids),
        )
        return {str(r["alpha_id"]): r for r in rows}
