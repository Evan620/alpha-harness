"""API keys, sealed at rest and rotated by remaining budget.

Free-tier quota is per account, so several keys add their budgets together. Rotation picks
by *headroom* rather than round-robin, which spreads load evenly and so is exactly wrong
when one key is nearly spent and another is untouched.

Keys are sealed with the same sealer as the BRAIN password and never leave the backend; the
UI only receives a masked hint.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from ..db.models import ApiKey, utcnow
from . import providers
from .budget import Headroom, Ledger, seconds_until_reset

if TYPE_CHECKING:
    from ..db.sqlite import Database
    from ..sealing import Sealer
    from .registry import ModelInfo, ModelRegistry

log = structlog.get_logger(__name__)

#: Domain separation for the sealer, matching the pattern used for the BRAIN credentials.
#: A blob sealed as an API key cannot be substituted in as a password, or the reverse.
KEY_CONTEXT = "google-api-key"


class LLMError(Exception):
    """Something wrong with the LLM setup, phrased for whoever has to fix it."""


class NoKeysError(LLMError):
    def __init__(self, provider: str | None = None) -> None:
        if provider and provider != "google":
            known = providers.get(provider)
            tail = (
                "That one bills your own account; the free providers above it do not."
                if known.paid
                else "Most of the providers listed there are free and need no card."
            )
            super().__init__(
                f"No {known.label} key has been added yet. Open AI Integration. {tail}"
            )
            return
        super().__init__(
            "No assistant key has been added yet. Get a free Google AI Studio key at "
            "https://aistudio.google.com/apikey, or pick another free provider under AI "
            "Integration. Keys from more than one account add their budgets together."
        )


class BudgetExhaustedError(LLMError):
    """Every key is out of budget for this model."""

    def __init__(self, model: str, states: list[Headroom]) -> None:
        daily = [s for s in states if s.blocked_by == "requests_per_day"]
        if daily and len(daily) == len(states):
            hours = seconds_until_reset() / 3600
            message = (
                f"Every key has used its daily allowance of {model}. It resets in about "
                f"{hours:.0f} hours (midnight Pacific). Switch to a model with a larger "
                "daily budget, or add another key."
            )
        else:
            wait = min((s.retry_after for s in states), default=60.0)
            message = (
                f"Every key is at its per-minute limit for {model}. Try again in about "
                f"{wait:.0f} seconds."
            )
        super().__init__(message)
        self.model = model
        self.states = states
        self.retry_after = min((s.retry_after for s in states), default=60.0)
        self.daily = bool(daily) and len(daily) == len(states)


def fingerprint(key: str) -> str:
    """Stable identity for a key, so the same one is not added twice.

    A hash, not the key: the database should not hold a second recoverable copy of a
    secret next to the sealed one.
    """
    return hashlib.sha256(key.strip().encode()).hexdigest()


def hint(key: str) -> str:
    """Enough of a key to recognise it, not enough to use it."""
    clean = key.strip()
    if len(clean) <= 10:
        return "…" + clean[-3:]
    return f"{clean[:6]}…{clean[-4:]}"


def serialise(row: ApiKey, usage: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "label": row.label,
        "provider": row.provider,
        "hint": row.hint,
        "enabled": row.enabled,
        "dailyLimit": row.daily_limit,
        "lastOkAt": row.last_ok_at.isoformat() if row.last_ok_at else None,
        "lastError": row.last_error,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "usage": usage or [],
    }


class KeyStore:
    """Holds the keys and decides which one to use next."""

    def __init__(self, db: Database, sealer: Sealer, ledger: Ledger) -> None:
        self.db = db
        self.sealer = sealer
        self.ledger = ledger

    # -- storage ---------------------------------------------------------

    async def add(
        self,
        key: str,
        label: str | None = None,
        provider: str = "google",
        daily_limit: int | None = None,
    ) -> ApiKey:
        clean = key.strip()
        if not clean:
            raise LLMError("That key is empty.")
        # A free tier stops on its own; a paid account stops when told to. Refusing the key
        # outright is the only point at which the app can be sure it was told — afterwards
        # the key is stored, and anything that reads it can spend money.
        if providers.get(provider).paid and not daily_limit:
            raise LLMError(
                f"Keys for {providers.get(provider).label} bill your own account, so they "
                "need a daily request cap. Set one and add the key again."
            )
        if daily_limit is not None and daily_limit < 1:
            raise LLMError("A daily cap has to be at least one request.")
        print_ = fingerprint(clean)

        async with self.db.session() as session:
            existing = await session.scalar(select(ApiKey).where(ApiKey.fingerprint == print_))
            if existing is not None:
                raise LLMError(
                    f"That key is already stored as {existing.label!r}. Adding it twice "
                    "would not increase your quota — quota is per account."
                )
            count = len(list((await session.scalars(select(ApiKey.id))).all()))
            row = ApiKey(
                label=label or f"Key {count + 1}",
                provider=provider,
                key_sealed=self.sealer.seal(clean, context=KEY_CONTEXT),
                hint=hint(clean),
                fingerprint=print_,
                daily_limit=daily_limit,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        log.info("llm.key.added", label=row.label, hint=row.hint, provider=provider)
        return row

    async def list_keys(self) -> list[ApiKey]:
        async with self.db.session() as session:
            return list((await session.scalars(select(ApiKey).order_by(ApiKey.id))).all())

    async def get(self, key_id: int) -> ApiKey | None:
        async with self.db.session() as session:
            return await session.get(ApiKey, key_id)

    async def secret(self, key_id: int) -> str:
        async with self.db.session() as session:
            row = await session.get(ApiKey, key_id)
            if row is None:
                raise LLMError(f"No key {key_id}.")
            return self.sealer.open(row.key_sealed, context=KEY_CONTEXT)

    async def set_enabled(
        self, key_id: int, enabled: bool, cap: int | None = None, *, clear: bool = False
    ) -> ApiKey:
        """Turn a key on or off, and optionally move its daily cap.

        The cap is editable because it is a spending limit: finding out it is too high is
        exactly the moment someone needs to lower it, and deleting and re-adding the key to
        do that is not a thing anyone will manage calmly. ``clear`` removes it entirely,
        which omitting ``cap`` cannot say — that already means "leave it alone".
        """
        if cap is not None and cap < 1:
            raise LLMError("A daily cap has to be at least one request.")
        async with self.db.session() as session:
            row = await session.get(ApiKey, key_id)
            if row is None:
                raise LLMError(f"No key {key_id}.")
            if clear and providers.get(row.provider).paid:
                # The cap is the only thing between this key and an open-ended bill. It can
                # be moved, never removed.
                raise LLMError(
                    f"Keys for {providers.get(row.provider).label} bill your own account, "
                    "so the daily cap cannot be removed. Raise it instead."
                )
            row.enabled = enabled
            if clear:
                row.daily_limit = None
            elif cap is not None:
                row.daily_limit = cap
            await session.commit()
            await session.refresh(row)
        return row

    async def remove(self, key_id: int) -> None:
        async with self.db.session() as session:
            row = await session.get(ApiKey, key_id)
            if row is None:
                raise LLMError(f"No key {key_id}.")
            await session.delete(row)
            await session.commit()
        self.ledger.forget(key_id)
        log.info("llm.key.removed", key_id=key_id)

    async def mark(self, key_id: int, *, error: str | None = None) -> None:
        """Record the outcome of a call, so a dead key is visible rather than mysterious."""
        async with self.db.session() as session:
            row = await session.get(ApiKey, key_id)
            if row is None:
                return
            if error is None:
                row.last_ok_at = utcnow()
                row.last_error = None
            else:
                row.last_error = error[:500]
            await session.commit()

    # -- rotation --------------------------------------------------------

    async def choose(self, model: ModelInfo, *, estimated_tokens: int = 4_000) -> int:
        """The key with the most daily budget left for this model.

        Most-remaining-first rather than round-robin, with ties broken on key id so the
        choice is reproducible.
        """
        # Provider first, budget second. A Groq key cannot answer for a Gemini model, so
        # offering it would spend a retry to learn something already known.
        rows = [r for r in await self.list_keys() if r.enabled and r.provider == model.provider]
        if not rows:
            raise NoKeysError(model.provider)

        states: list[Headroom] = [
            await self.ledger.allows(
                row.id, model, estimated_tokens=estimated_tokens, cap=row.daily_limit
            )
            for row in rows
        ]

        usable = [s for s in states if s.available]
        if not usable:
            raise BudgetExhaustedError(model.id, states)

        best = max(usable, key=lambda s: (s.daily_remaining, -s.key_id))
        return best.key_id

    async def status(self, registry: ModelRegistry) -> dict[str, Any]:
        """Keys, their health, and what budget remains — the whole picture in one call."""
        rows = await self.list_keys()
        usage = await self.ledger.usage([r.id for r in rows])
        by_key: dict[int, list[dict[str, Any]]] = {}
        for entry in usage:
            by_key.setdefault(int(entry["keyId"]), []).append(entry)

        configured_providers = {r.provider for r in rows}
        text_models = registry.all("text")
        budget: list[dict[str, Any]] = []
        for model in text_models:
            if model.provider not in configured_providers:
                continue
            usable = [r for r in rows if r.enabled and r.provider == model.provider]
            remaining = 0
            # Per key, because a paid key's ceiling is the user's own and two of them need
            # not agree. For the free providers every key shares the model's number and this
            # collapses back to it.
            # Starts at the model's own number so a model whose keys are all disabled still
            # shows what it would allow, rather than a ceiling of zero.
            per_key = model.rpd
            for row in usable:
                state = await self.ledger.headroom(row.id, model, cap=row.daily_limit)
                remaining += state.daily_remaining
                per_key = max(per_key, state.requests_per_day)
            budget.append(
                {
                    "model": model.id,
                    "label": model.label,
                    "provider": model.provider,
                    "perKeyPerDay": per_key,
                    "remainingToday": remaining,
                    "bulk": model.bulk,
                }
            )

        return {
            "keys": [serialise(r, by_key.get(r.id, [])) for r in rows],
            "enabled": sum(1 for r in rows if r.enabled),
            "budget": budget,
            "resetInSeconds": round(seconds_until_reset()),
            "quotaTimezone": "America/Los_Angeles",
        }
