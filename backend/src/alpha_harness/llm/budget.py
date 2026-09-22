"""Tracking what each key has spent.

Google publishes no remaining-quota endpoint, so the only way to rotate keys sensibly is
to count locally. Three windows, tracked differently because they behave differently:

* **RPM and TPM** are sliding sixty-second windows, kept in memory: losing them on a
  restart costs at most one minute of over-caution.
* **RPD** is a calendar day and *must* survive a restart, or a spent key looks fresh and
  every rotation walks into a ``429``.

**The day boundary is Pacific, not UTC.** AI Studio quotas reset at midnight
America/Los_Angeles, and counting UTC days would hand a key's daily budget back seven or
eight hours early.

Accounting is deliberately conservative — a request is refused here when it *would* exceed
a limit — because a local refusal can name another key or model, and a ``429`` cannot.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from datetime import time as clock  # `time` is already the module, imported above
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from ..db.models import KeyUsage, utcnow

if TYPE_CHECKING:
    from ..db.sqlite import Database
    from .registry import ModelInfo

log = structlog.get_logger(__name__)

#: Where Google's daily quota clock lives.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")

WINDOW_SECONDS = 60.0

#: Assumed prompt size when a caller cannot estimate one. Enough to stop a large request
#: slipping under a TPM check that then fails on the wire.
DEFAULT_TOKEN_ESTIMATE = 4_000


def quota_day(moment: datetime | None = None) -> str:
    """The quota day a moment falls in, as ``YYYY-MM-DD`` in Pacific time."""
    return (moment or utcnow()).astimezone(QUOTA_TZ).strftime("%Y-%m-%d")


def seconds_until_reset(moment: datetime | None = None, tz: tzinfo = QUOTA_TZ) -> float:
    """How long until a daily budget comes back.

    Built from the next local *date* rather than by adding 24 hours, so the two days a year
    that are 23 or 25 hours long do not shift the answer. ``tz`` picks the clock: Pacific
    for the assistant's quota, US Eastern for the platform's simulation allowance.
    """
    now = (moment or utcnow()).astimezone(tz)
    midnight = datetime.combine(now.date() + timedelta(days=1), clock.min, tzinfo=tz)
    return max(0.0, (midnight - now).total_seconds())


@dataclass(slots=True)
class Window:
    """A sliding sixty-second record of requests and tokens."""

    events: deque[tuple[float, int]] = field(default_factory=deque)

    def trim(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def add(self, tokens: int, now: float) -> None:
        self.events.append((now, tokens))
        self.trim(now)

    def requests(self, now: float) -> int:
        self.trim(now)
        return len(self.events)

    def tokens(self, now: float) -> int:
        self.trim(now)
        return sum(t for _, t in self.events)

    def next_free(self, now: float) -> float:
        """Seconds until the oldest event ages out of the window."""
        self.trim(now)
        if not self.events:
            return 0.0
        return max(0.0, WINDOW_SECONDS - (now - self.events[0][0]))


@dataclass(slots=True)
class Headroom:
    """What is left on one (key, model) pair, and what is blocking it."""

    key_id: int
    model: str
    requests_today: int
    requests_per_day: int
    requests_this_minute: int
    requests_per_minute: int
    tokens_this_minute: int
    tokens_per_minute: int
    blocked_by: str | None = None
    #: Seconds until the block clears. Large for a daily limit, small for a per-minute one.
    retry_after: float = 0.0

    @property
    def available(self) -> bool:
        return self.blocked_by is None

    @property
    def daily_remaining(self) -> int:
        return max(0, self.requests_per_day - self.requests_today)

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyId": self.key_id,
            "model": self.model,
            "available": self.available,
            "blockedBy": self.blocked_by,
            "retryAfter": round(self.retry_after, 1),
            "requestsToday": self.requests_today,
            "requestsPerDay": self.requests_per_day,
            "dailyRemaining": self.daily_remaining,
            "requestsThisMinute": self.requests_this_minute,
            "requestsPerMinute": self.requests_per_minute,
            "tokensThisMinute": self.tokens_this_minute,
            "tokensPerMinute": self.tokens_per_minute,
        }


class Ledger:
    """Per-key, per-model usage accounting."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._windows: dict[tuple[int, str], Window] = {}
        #: Daily counts, read through from the database on first use and kept in step
        #: with it afterwards, so the hot path does not hit SQLite per check.
        self._daily: dict[tuple[int, str, str], int] = {}

    def forget(self, key_id: int) -> None:
        """Drop a removed key's counts: SQLite hands its id to the next key added."""
        self._windows = {k: v for k, v in self._windows.items() if k[0] != key_id}
        self._daily = {k: v for k, v in self._daily.items() if k[0] != key_id}

    def _window(self, key_id: int, model: str) -> Window:
        return self._windows.setdefault((key_id, model), Window())

    async def _requests_today(self, key_id: int, model: str) -> int:
        day = quota_day()
        cached = self._daily.get((key_id, model, day))
        if cached is not None:
            return cached
        async with self.db.session() as session:
            row = await session.scalar(
                select(KeyUsage).where(
                    KeyUsage.api_key_id == key_id,
                    KeyUsage.model == model,
                    KeyUsage.day == day,
                )
            )
            count = int(row.requests) if row else 0
        self._daily[(key_id, model, day)] = count
        return count

    async def headroom(self, key_id: int, model: ModelInfo, *, cap: int | None = None) -> Headroom:
        """What is left, and the first limit that would stop the next request.

        ``cap`` is the key's own daily limit and wins over the model's when it is set. Two
        things need that. A paid key has no published daily ceiling, so the user names one
        and it is the only thing standing between them and a bill. And a *discovered* model
        carries :data:`UNKNOWN_LIMITS`, deliberately tiny — correct for a free tier nobody
        has documented, and absurd on an account that is being billed per token.
        """
        now = time.monotonic()
        window = self._window(key_id, model.id)
        today = await self._requests_today(key_id, model.id)
        daily = cap if cap is not None else model.rpd

        state = Headroom(
            key_id=key_id,
            model=model.id,
            requests_today=today,
            requests_per_day=daily,
            requests_this_minute=window.requests(now),
            requests_per_minute=model.rpm,
            tokens_this_minute=window.tokens(now),
            tokens_per_minute=model.tpm,
        )

        # Daily first: it is the one that cannot be waited out in any useful sense. Zero is
        # "no daily ceiling", not "already spent": a paid model publishes none, and reading
        # it literally would refuse the day's very first request.
        if daily > 0 and today >= daily:
            state.blocked_by = "requests_per_day"
            state.retry_after = seconds_until_reset()
        elif state.requests_this_minute >= model.rpm:
            state.blocked_by = "requests_per_minute"
            state.retry_after = window.next_free(now)
        elif state.tokens_this_minute >= model.tpm:
            state.blocked_by = "tokens_per_minute"
            state.retry_after = window.next_free(now)
        return state

    async def allows(
        self,
        key_id: int,
        model: ModelInfo,
        *,
        estimated_tokens: int = DEFAULT_TOKEN_ESTIMATE,
        cap: int | None = None,
    ) -> Headroom:
        """Headroom, also refusing a request whose *size* would breach TPM.

        Nothing is reserved between this check and :meth:`record`, so callers checking at
        once can overshoot a limit by their number.
        """
        state = await self.headroom(key_id, model, cap=cap)
        if state.available and state.tokens_this_minute + estimated_tokens > model.tpm:
            state.blocked_by = "tokens_per_minute"
            state.retry_after = self._window(key_id, model.id).next_free(time.monotonic())
        return state

    async def record(self, key_id: int, model: str, tokens: int) -> None:
        """Count a request that actually went out.

        Called after the response so the token count is real rather than estimated —
        including thinking tokens, which count against TPM and are easy to forget.
        """
        now = time.monotonic()
        self._window(key_id, model).add(max(0, tokens), now)

        day = quota_day()
        spent, now_at = max(0, tokens), utcnow()
        # One atomic statement: calls finishing together otherwise read-then-write the same
        # count and lose increments, and the day's first two collide inserting its row.
        statement = (
            insert(KeyUsage)
            .values(
                api_key_id=key_id,
                model=model,
                day=day,
                requests=1,
                tokens=spent,
                last_request_at=now_at,
            )
            .on_conflict_do_update(
                index_elements=["api_key_id", "model", "day"],
                set_={
                    "requests": KeyUsage.requests + 1,
                    "tokens": KeyUsage.tokens + spent,
                    "last_request_at": now_at,
                },
            )
            .returning(KeyUsage.requests)
        )
        async with self.db.session() as session:
            requests = (await session.execute(statement)).scalar_one()
        self._daily[(key_id, model, day)] = requests

        # Yesterday's counts are not just stale, they are wrong to serve — drop them.
        for cached in [k for k in self._daily if k[2] != day]:
            del self._daily[cached]

    async def penalise(
        self, key_id: int, model: ModelInfo, *, daily: bool, cap: int | None = None
    ) -> None:
        """Believe the provider over our own arithmetic.

        A ``429`` means the local count was wrong, so the count is moved to the ceiling and
        rotation immediately treats this pair as spent rather than retrying into it.

        ``cap`` is the key's own ceiling, as in :meth:`headroom`. Without it a paid model,
        whose published ``rpd`` is zero, would be moved to a ceiling of zero — which is to
        say not moved at all, and rotation would retry into the same 429 for good.
        """
        # One past what it has, when there is no published ceiling: the point is to mark
        # this pair spent, and a ceiling of zero would mark nothing.
        ceiling = cap if cap is not None else model.rpd
        if daily:
            day = quota_day()
            async with self.db.session() as session:
                row = await session.scalar(
                    select(KeyUsage).where(
                        KeyUsage.api_key_id == key_id,
                        KeyUsage.model == model.id,
                        KeyUsage.day == day,
                    )
                )
                if row is None:
                    # Explicit zeros: column defaults are only applied at flush, and this
                    # row is read back before then.
                    row = KeyUsage(api_key_id=key_id, model=model.id, day=day, requests=0, tokens=0)
                    session.add(row)
                row.requests = max(row.requests or 0, ceiling or (row.requests or 0) + 1)
                await session.commit()
                self._daily[(key_id, model.id, day)] = row.requests
            log.warning("llm.budget.daily_exhausted", key_id=key_id, model=model.id)
        else:
            now = time.monotonic()
            window = self._window(key_id, model.id)
            while window.requests(now) < model.rpm:
                window.add(0, now)
            log.info("llm.budget.minute_exhausted", key_id=key_id, model=model.id)

    async def usage(self, key_ids: list[int]) -> list[dict[str, Any]]:
        """Today's spend per key and model, for the keys panel."""
        if not key_ids:
            return []
        day = quota_day()
        async with self.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(KeyUsage).where(
                            KeyUsage.api_key_id.in_(key_ids), KeyUsage.day == day
                        )
                    )
                ).all()
            )
        return [
            {
                "keyId": r.api_key_id,
                "model": r.model,
                "day": r.day,
                "requests": r.requests,
                "tokens": r.tokens,
                "lastRequestAt": r.last_request_at.isoformat() if r.last_request_at else None,
            }
            for r in rows
        ]
