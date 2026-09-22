"""Correlation Breaker: re-shape a submittable Alpha that BRAIN says is already in the pool.

Failing ``PROD_CORRELATION`` means the daily PnL is Pearson-correlated at 0.70 or more with an
Alpha already in production, and this one's Sharpe is not 10% above it. The signal is sound; it
is the *exposure* that is shared. So the settings are never touched here — region, delay,
universe, neutralization, decay and truncation are the source Alpha's, exactly — and only the
expression changes.

Every recipe binds the original expression to one name, ``alpha``, and wraps that. Binding is
appended after the reader's own statements, so it cannot disturb them: FastExpr runs statements
in order, and a name rebound at the end has already been read by everything above it.

The recipes come from what the platform's own guidance says actually works — orthogonalize
against the crowded vectors, neutralize inside finer clusters, trade on catalysts rather than
every day, reshape the distribution — and never from adding noise, which BRAIN forbids outright
and which decays out of sample anyway.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..brain.schemas import SimulationRequest, SimulationSettings
from ..labs.fastexpr import Node, ParseError, parse, render
from ..labs.launch import OPERATORS_UNREAD, account_operators

if TYPE_CHECKING:
    from collections.abc import Sequence

#: What the original expression is bound to. Appending the binding makes it safe whatever the
#: reader already called their own variables.
ALPHA = "alpha"


@dataclass(frozen=True, slots=True)
class Compressed:
    """One Alpha's expression, reduced to statements plus the name holding its value."""

    #: The reader's own statements, canonically rendered, in order.
    prelude: tuple[str, ...]
    #: The line binding :data:`ALPHA`, or empty when the program already ends by assigning it.
    binding: str

    def program(self, tail: str, extra: Sequence[str] = ()) -> str:
        """The whole expression: the reader's work, the binding, any recipe setup, then ``tail``."""
        lines = [*self.prelude]
        if self.binding:
            lines.append(self.binding)
        lines.extend(extra)
        lines.append(tail)
        return "\n".join(f"{line};" if i < len(lines) - 1 else line for i, line in enumerate(lines))

    @property
    def bound(self) -> str:
        """The reader's own work plus the binding: what ``alpha`` means, shown once.

        The recipes are displayed against this rather than repeating it, so a card carries
        only the line that is actually the idea.
        """
        lines = [*self.prelude, *([self.binding] if self.binding else [])]
        return "\n".join(f"{line};" for line in lines)

    @staticmethod
    def transform(tail: str, extra: Sequence[str] = ()) -> str:
        """Just the re-shape, written against ``alpha``."""
        lines = [*extra, tail]
        return "\n".join(f"{line};" if i < len(lines) - 1 else line for i, line in enumerate(lines))


def compress(expression: str) -> Compressed:
    """Reduce an expression to its statements and a single name holding its value.

    ``rank(close)`` becomes ``alpha = rank(close)``. A multi-statement program keeps every
    statement and gains one more. A program whose last statement already assigns a name is
    bound from that name rather than from a copy of its right-hand side, so nothing is
    computed twice.
    """
    tree = parse(expression)
    statements: tuple[Node, ...] = tree.args if tree.kind == "seq" else (tree,)
    if not statements:
        raise ParseError("The expression is empty.")

    last = statements[-1]
    if last.kind == "assign":
        # The program's value is whatever it last assigned; keep every statement as written.
        prelude = tuple(render(s) for s in statements)
        binding = "" if last.value == ALPHA else f"{ALPHA} = {last.value}"
        return Compressed(prelude=prelude, binding=binding)

    prelude = tuple(render(s) for s in statements[:-1])
    return Compressed(prelude=prelude, binding=f"{ALPHA} = {render(last)}")


@dataclass(frozen=True, slots=True)
class Recipe:
    """One way to re-expose the same signal."""

    id: str
    name: str
    #: Why it breaks correlation, in the reader's terms.
    why: str
    #: Statements the recipe needs before its own line, already using :data:`ALPHA`.
    setup: tuple[str, ...]
    #: The expression the simulation runs.
    tail: str
    operators: tuple[str, ...]
    #: Data and grouping fields it reads, checked against the market before it is offered.
    fields: tuple[str, ...] = ()
    #: What it costs, said before it is run rather than discovered afterwards.
    caution: str = ""


RECIPES: tuple[Recipe, ...] = (
    Recipe(
        id="neut_size",
        name="Orthogonalize against size",
        why=(
            "Projects out market capitalisation, the single most shared exposure in the pool. "
            "What is left is the part of the signal that is not a size bet."
        ),
        setup=(),
        tail=f"vector_neut({ALPHA}, log(cap))",
        operators=("vector_neut", "log"),
        fields=("cap",),
    ),
    Recipe(
        id="neut_momentum",
        name="Orthogonalize against short-term momentum",
        why=(
            "Forces the daily exposure to five-day price momentum to zero. Fundamental and "
            "analyst signals often ride momentum without meaning to."
        ),
        setup=(),
        tail=f"vector_neut({ALPHA}, ts_delta(close, 5))",
        operators=("vector_neut", "ts_delta"),
        fields=("close",),
    ),
    Recipe(
        id="neut_both",
        name="Orthogonalize against size and momentum, then decay",
        why=(
            "Both crowded vectors projected out in turn, then smoothed. The decay is there to "
            "settle the turnover that orthogonalizing stirs up."
        ),
        setup=(
            f"no_size = vector_neut({ALPHA}, log(cap))",
            "orthogonal = vector_neut(no_size, ts_delta(close, 5))",
        ),
        tail="ts_decay_linear(orthogonal, 3)",
        operators=("vector_neut", "log", "ts_delta", "ts_decay_linear"),
        fields=("cap", "close"),
        caution="Orthogonalizing lifts turnover; the decay is what keeps it inside the limit.",
    ),
    Recipe(
        id="cluster_zscore",
        name="Normalize inside sector and size clusters",
        why=(
            "Scores each instrument against peers of the same sector and size band rather than "
            "against the whole market. The pool almost always neutralizes on sector alone."
        ),
        setup=(
            'cap_bucket = bucket(rank(cap), buckets = "0.33, 0.66")',
            "cluster = densify(group_cartesian_product(sector, cap_bucket))",
        ),
        tail=f"group_zscore({ALPHA}, cluster)",
        operators=("bucket", "rank", "densify", "group_cartesian_product", "group_zscore"),
        fields=("cap", "sector"),
        caution=(
            "Three coarse bands on sector, not subindustry: finer clusters leave too few "
            "instruments in a bucket and fail the weight test."
        ),
    ),
    Recipe(
        id="event_gate",
        name="Trade only on volume catalysts",
        why=(
            "Holds its weights between catalysts instead of re-ranking every day. A PnL that "
            "moves on event days cannot track a continuously rebalanced model."
        ),
        setup=("catalyst = volume > 1.25 * adv20",),
        tail=f"trade_when(catalyst, {ALPHA}, -1)",
        operators=("trade_when",),
        fields=("volume", "adv20"),
        caution="Gating cuts turnover, which must stay above the 1% floor to be submittable.",
    ),
    Recipe(
        id="regime_soft",
        name="Weight by volume regime",
        why=(
            "Leans into the signal when volume is unusual and backs off when it is quiet, "
            "without ever going flat. A softer version of catalyst gating."
        ),
        setup=(f"modulated = {ALPHA} * rank(volume / adv20)",),
        tail="hump(modulated, hump = 0.01)",
        operators=("rank", "hump"),
        fields=("volume", "adv20"),
    ),
    Recipe(
        id="reshape",
        name="Reshape the weight distribution",
        why=(
            "Caps the outliers, compresses the tails and re-centres. Two Alphas can rank "
            "instruments identically and still hold very different books."
        ),
        setup=(
            f"capped = winsorize({ALPHA}, std = 3)",
            "compressed = signed_power(capped, 0.5)",
        ),
        tail="normalize(compressed, useStd = true, limit = 2.5)",
        operators=("winsorize", "signed_power", "normalize"),
        caution=(
            "Compressing rather than expanding the tails on purpose: expanding them "
            "concentrates the book and fails the maximum-weight test."
        ),
    ),
    Recipe(
        id="fast_innovation",
        name="Trade the innovation, not the level",
        why=(
            "Subtracts the signal's own twenty-day mean, leaving the part that is new. The "
            "pool is full of models trading the level of the same quantity."
        ),
        setup=(),
        tail=f"{ALPHA} - ts_mean({ALPHA}, 20)",
        operators=("ts_mean",),
    ),
    Recipe(
        id="heavy_tail",
        name="Remap onto a heavy-tailed distribution",
        why=(
            "Maps the signal through a Cauchy quantile, so conviction in the tails carries "
            "weight the pool's Gaussian normalisation spreads across the middle."
        ),
        setup=(),
        tail=f'quantile({ALPHA}, driver = "cauchy", sigma = 1.0)',
        operators=("quantile",),
        caution=(
            "The fattest-tailed of these. Watch the maximum-weight test: one extreme "
            "instrument can take a quarter of the book."
        ),
    ),
)


def offered(
    recipes: Sequence[Recipe], operators: set[str], fields: set[str]
) -> list[tuple[Recipe, str]]:
    """Each recipe with the reason it cannot run, empty when it can.

    Reported rather than filtered away: a recipe missing because the market has no ``adv20``
    is worth knowing about, and a silently shorter list reads as if there were fewer ideas.
    """
    out: list[tuple[Recipe, str]] = []
    for recipe in recipes:
        missing_ops = [o for o in recipe.operators if o not in operators]
        missing_fields = [f for f in recipe.fields if f not in fields]
        if missing_ops:
            out.append((recipe, f"needs {', '.join(missing_ops)}, not on this account"))
        elif missing_fields:
            out.append((recipe, f"needs {', '.join(missing_fields)}, not in this market"))
        else:
            out.append((recipe, ""))
    return out


def requests(
    compressed: Compressed, recipes: Sequence[Recipe], settings: dict[str, Any]
) -> list[SimulationRequest]:
    """One simulation per recipe, every one at the source Alpha's own settings.

    The settings are copied whole and never edited: the whole point is to hold everything
    the Alpha was judged on and vary only what it is exposed to.
    """
    held = SimulationSettings.model_validate(settings)
    return [
        SimulationRequest(settings=held.model_copy(), regular=compressed.program(r.tail, r.setup))
        for r in recipes
    ]


#: The check this tool exists to get past.
PROD_CORRELATION = "PROD_CORRELATION"


def _correlation(checks: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """BRAIN's own production-correlation check, as it last reported it."""
    for check in checks:
        if str(check.get("name", "")).upper() == PROD_CORRELATION:
            return check
    return None


async def plan(state: Any, alpha_id: str) -> dict[str, Any]:
    """Everything the screen shows: the Alpha, its settings, and each recipe's expression.

    Reads BRAIN and the local catalog; simulates nothing.
    """
    problems: list[str] = []
    body = await state.endpoints.alpha_body(alpha_id)
    code = body.get("regular") or body.get("combo") or body.get("selection") or {}
    expression = code.get("code") if isinstance(code, dict) else None
    settings = dict(body.get("settings") or {})
    checks = ((body.get("is") or {}).get("checks")) or []

    if not expression:
        problems.append(f"{alpha_id} has no expression to re-shape.")
        return _empty(alpha_id, settings, checks, problems)
    try:
        compressed = compress(expression)
    except ParseError as exc:
        problems.append(f"Its expression could not be parsed, so it cannot be re-shaped: {exc}")
        return _empty(alpha_id, settings, checks, problems)

    region, delay = str(settings.get("region") or ""), int(settings.get("delay") or 0)
    universe = str(settings.get("universe") or "")
    operators = {
        str(o.get("name")) for o in await account_operators(state, refresh=False) if o.get("name")
    }
    if not operators:
        problems.append(OPERATORS_UNREAD)

    # A recipe reads fields of its own; one the market does not carry is said so, not hidden.
    wanted = sorted({f for recipe in RECIPES for f in recipe.fields})
    held = await asyncio.gather(*(state.queries.field_availability(f) for f in wanted))
    fields = {
        name
        for name, rows in zip(wanted, held, strict=True)
        if any(
            r["region"] == region and int(r["delay"]) == delay and r["universe"] == universe
            for r in rows
        )
    }

    rows = []
    for recipe, blocked in offered(RECIPES, operators, fields):
        rows.append(
            {
                "id": recipe.id,
                "name": recipe.name,
                "why": recipe.why,
                "caution": recipe.caution,
                "transform": compressed.transform(recipe.tail, recipe.setup),
                "blocked": blocked,
            }
        )
    return {
        "alphaId": alpha_id,
        "expression": expression,
        #: What ``alpha`` stands for, shown once above the re-shapes.
        "bound": compressed.bound,
        "settings": _held(settings),
        #: The Alpha's settings whole, which is what every simulation is built from.
        "rawSettings": settings,
        "correlation": _correlation(checks),
        "recipes": rows,
        "problems": problems,
    }


def _held(settings: dict[str, Any]) -> dict[str, Any]:
    """The settings every simulation runs at, which are the Alpha's own and are not editable."""
    return {
        "region": settings.get("region"),
        "delay": settings.get("delay"),
        "universe": settings.get("universe"),
        "neutralization": settings.get("neutralization"),
        "decay": settings.get("decay"),
        "truncation": settings.get("truncation"),
    }


def _empty(
    alpha_id: str, settings: dict[str, Any], checks: Sequence[dict[str, Any]], problems: list[str]
) -> dict[str, Any]:
    return {
        "alphaId": alpha_id,
        "expression": "",
        "bound": "",
        "settings": _held(settings),
        "rawSettings": settings,
        "correlation": _correlation(checks),
        "recipes": [],
        "problems": problems,
    }
