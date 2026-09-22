"""Async HTTP client for the BRAIN API.

Two behaviours in here are the reason this file exists, and both break naive clients:

1. **A response carrying ``Retry-After`` means "not ready yet".** The header's presence —
   not the status code — is the signal (``docs/wqb-api/03-conventions.md``).
   :meth:`BrainClient.poll` re-issues the request until it is gone; recordsets,
   correlations and checks are read that way.
2. **``POST /simulations`` answers in headers.** The id is in ``Location``; the caller
   reads it from :class:`BrainResponse`.

Versioning lives in the ``Accept`` header (``application/json;version=N``), not the path.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx
import structlog

from .errors import (
    DAILY_LIMIT_DETAIL,
    BrainAuthError,
    BrainDailyLimitReached,
    BrainError,
    BrainForbidden,
    BrainNotFound,
    BrainPollTimeout,
    BrainRateLimited,
    BrainServerError,
    BrainServiceUnavailable,
    BrainTransportError,
    BrainValidationError,
    BrainVerificationRequired,
)

log = structlog.get_logger(__name__)

Method = Literal["GET", "POST", "PATCH", "DELETE", "OPTIONS"]

DEFAULT_VERSION = "2.0"

#: Shortest wait between polls of a pending job, whatever ``Retry-After`` says.
MIN_POLL_DELAY = 0.25

# There is deliberately no alpha-submission method in endpoints.py. BrainClient.request,
# however, accepts an unconstrained method and path, and poll() documents the submission
# verb flip. These local blocks make that omission an enforceable boundary. OPTIONS is not
# generally guarded because the settings and alpha-filter schema readers depend on it.
GUARDED_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BLOCKED_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^alphas/[^/]+/submit$"),
    re.compile(r"^alphas$"),
    re.compile(r"^users/[^/]+/alphas$"),
)


def _normalise_path(path: str) -> str:
    """Return a case-folded, decoded path with dot segments removed."""
    parsed = urlsplit(path)
    raw_path = parsed.path if parsed.scheme or parsed.netloc else path.split("?", 1)[0]
    raw_path = raw_path.split("#", 1)[0]

    # Measured with httpx 0.28.1: dot segments leave as their resolved path, absolute
    # URLs bypass base_url, and percent escapes remain encoded. Decode twice so a server
    # cannot turn a double-encoded spelling into a route that escaped this guard.
    decoded = unquote(unquote(raw_path))
    parts: list[str] = []
    for part in decoded.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts).casefold()


#: A reset this large is a wall-clock instant, not a countdown.
EPOCH_SECONDS = 1e9
#: Longest gap a response is allowed to talk us into. Not a rate limit — a bound on how
#: badly a misread header may stall a sync.
MAX_MEASURED_GAP = 60.0


def _header_num(headers: httpx.Headers, name: str) -> float | None:
    raw = headers.get(name)
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Daily simulation quota from the ``x-ratelimit-*`` headers of ``POST /simulations``.

    Undocumented, but sent on every send: limit, remaining, and reset in seconds.
    """

    limit: int | None
    remaining: int | None
    reset_seconds: float | None
    observed_at: float

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> RateLimit | None:
        def _num(name: str, kind: type[int] | type[float]) -> Any:
            raw = headers.get(name)
            try:
                return kind(raw) if raw is not None else None
            except ValueError:
                return None

        limit = _num("x-ratelimit-limit", int)
        remaining = _num("x-ratelimit-remaining", int)
        reset = _num("x-ratelimit-reset", float)
        if limit is None and remaining is None and reset is None:
            return None
        return cls(limit=limit, remaining=remaining, reset_seconds=reset, observed_at=time.time())


@dataclass(slots=True)
class BrainResponse:
    """A completed BRAIN response with the bits callers actually need."""

    status: int
    headers: httpx.Headers
    body: Any
    retry_after: float | None
    rate_limit: RateLimit | None
    location: str | None

    @property
    def pending(self) -> bool:
        """True while the server is still working on an asynchronous job."""
        return self.retry_after is not None


def _parse_retry_after(headers: httpx.Headers) -> float | None:
    """Seconds to wait, or ``None`` when the header is absent and the result is ready.

    Exactly the documented parser (``docs/wqb-api/endpoints/osmosis.md``): numeric seconds,
    or an HTTP-date; zero, negative, a past date or anything unreadable means poll now.
    """
    raw = headers.get("retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds()
        except TypeError, ValueError:
            return 0.0
    if not math.isfinite(seconds):
        return 0.0
    return max(seconds, 0.0)


class BrainRequestBlocked(BrainError):  # noqa: N818
    """Refused locally before the request was sent. Never reaches the network."""

    retryable = False


class BrainClient:
    """Cookie-authenticated async client.

    The instance owns a cookie jar; :meth:`export_cookies` / :meth:`load_cookies` persist
    it so a restart does not cost another proof-of-work solve.
    """

    def __init__(
        self,
        base_url: str = "https://api.worldquantbrain.com",
        *,
        timeout: float = 30.0,
        poll_timeout: float = 300.0,
        default_attempts: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self._default_attempts = max(1, default_attempts)
        self._poll_timeout = poll_timeout
        self._timeout = timeout
        #: Seconds between sends per endpoint, learned from its own rate-limit headers, and
        #: the next free slot in each. Measured: BRAIN meters each endpoint separately, and
        #: asking faster than it allows buys 429s rather than throughput.
        self._gap: dict[str, float] = {}
        self._next_send: dict[str, float] = {}
        #: Set once an endpoint's first response has reported its window, so the callers
        #: behind it pace on measurements rather than on an assumption.
        self._measured: dict[str, asyncio.Event] = {}
        self._pace_lock = asyncio.Lock()
        #: Monotonic time before which no request is sent: set by a ``429`` so every caller
        #: backs off together instead of each retrying into a server that said stop.
        self._resume_at = 0.0
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "alpha-harness/0.1 (local research studio)"},
        )

    # -- lifecycle -------------------------------------------------------

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- cookie jar ------------------------------------------------------

    def export_cookies(self) -> list[dict[str, Any]]:
        """Serialise the jar for storage."""
        return [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "expires": c.expires,
                "secure": c.secure,
            }
            for c in self._client.cookies.jar
        ]

    def load_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Restore a jar produced by :meth:`export_cookies`."""
        for c in cookies:
            if not c.get("name") or c.get("value") is None:
                continue
            self._client.cookies.set(
                c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/")
            )

    def clear_cookies(self) -> None:
        self._client.cookies.clear()

    # -- core request ----------------------------------------------------

    @staticmethod
    def _bucket(path: str) -> str:
        """The endpoint a path is metered under: ``/simulations/{id}`` shares ``simulations``."""
        return path.strip("/").split("/", 1)[0]

    async def _reserve(self, bucket: str) -> None:
        """Hold the caller until this endpoint's next free slot.

        Nothing about the rate is written here. The first caller to reach an unmeasured
        endpoint goes straight through and every other waits for what its headers report,
        rather than guessing a number that would be wrong for someone else's account.
        """
        async with self._pace_lock:
            measured = self._measured.get(bucket)
            if measured is None:
                self._measured[bucket] = asyncio.Event()
                return
        if not measured.is_set():
            await measured.wait()

        async with self._pace_lock:
            gap = self._gap.get(bucket, 0.0)
            if gap <= 0:
                return
            slot = max(time.monotonic(), self._next_send.get(bucket, 0.0))
            self._next_send[bucket] = slot + gap
        if (wait := slot - time.monotonic()) > 0:
            await asyncio.sleep(wait)

    def _measure(self, bucket: str, headers: httpx.Headers) -> None:
        """Read this endpoint's window off the response: how many, and how long until reset."""
        limit = _header_num(headers, "ratelimit-limit")
        reset = _header_num(headers, "ratelimit-reset")
        if reset is not None and reset > EPOCH_SECONDS:
            # Some gateways report the moment the window rolls over rather than how long
            # until it does. Taken literally that is a sleep measured in decades.
            reset = max(0.0, reset - time.time())
        if limit and limit > 0 and reset and 0 < reset <= MAX_MEASURED_GAP:
            # Paces starts, not finishes. ``remaining`` is deliberately unused: a one-slot
            # window reports nothing left after every request, and deferring from the moment
            # a slow download *ends* would queue the next start behind its transfer.
            self._gap[bucket] = reset / limit
        else:
            # Not metered this way, or a window we cannot make sense of. Left unpaced and
            # left to the Retry-After gate, which is safer than obeying a nonsense number.
            self._gap[bucket] = 0.0

    def _guard(self, method: Method, path: str) -> None:
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc:
            base = urlsplit(self.base_url)
            try:
                target_scheme = (parsed.scheme or base.scheme).casefold()
                target_port = parsed.port or {"http": 80, "https": 443}.get(target_scheme)
                base_scheme = base.scheme.casefold()
                base_port = base.port or {"http": 80, "https": 443}.get(base_scheme)
            except ValueError as exc:
                raise BrainRequestBlocked(
                    "alpha-harness refused to send this request: the absolute URL has an "
                    "invalid port and cannot be verified against the configured BRAIN origin."
                ) from exc

            if (
                target_scheme != base_scheme
                or (parsed.hostname or "").casefold() != (base.hostname or "").casefold()
                or target_port != base_port
            ):
                raise BrainRequestBlocked(
                    "alpha-harness refused to send this request: an absolute URL outside the "
                    "configured BRAIN origin could exfiltrate credentials supplied by "
                    "brain/auth.py:158."
                )

        verb = method.upper()
        normalised = _normalise_path(path)
        submit = BLOCKED_PATHS[0].fullmatch(normalised) is not None
        other_blocked = any(pattern.fullmatch(normalised) for pattern in BLOCKED_PATHS[1:])
        if (submit and verb != "GET") or (verb in GUARDED_METHODS and other_blocked):
            raise BrainRequestBlocked(
                "alpha-harness refused to send this request: alpha submission and bulk alpha "
                "mutation are disabled by design."
            )

    async def request(
        self,
        method: Method,
        path: str,
        *,
        version: str = DEFAULT_VERSION,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        raise_for_status: bool = True,
        raw: bool = False,
        read_timeout: float | None = None,
    ) -> BrainResponse:
        """Issue one request and translate failures into typed exceptions.

        The alpha-submit guard is applied locally before throttling or HTTP work.
        ``raw`` leaves a successful body as undecoded ``bytes``, for payloads whose caller
        has a faster decoder than the standard library's. ``read_timeout`` overrides the client's
        for one call, since a few endpoints legitimately take longer than the rest.

        ``params`` values that are ``None`` are dropped.
        """
        self._guard(method, path)

        merged = {"Accept": f"application/json;version={version}"}
        if headers:
            merged.update(headers)

        clean_params = {k: v for k, v in params.items() if v is not None} if params else None

        # Re-checked after each sleep: another 429 may have pushed the pause further out.
        # Jittered so every waiter does not re-send in the same tick and draw another 429.
        while (wait := self._resume_at - time.monotonic()) > 0:  # noqa: ASYNC110
            await asyncio.sleep(wait + random.uniform(0.05, 0.25))

        bucket = self._bucket(path)
        await self._reserve(bucket)

        try:
            response = await self._client.request(
                method,
                path.lstrip("/"),
                params=clean_params,
                json=json_body,
                headers=merged,
                auth=auth or httpx.USE_CLIENT_DEFAULT,
                # Only the read is stretched: passing read_timeout as the default would
                # make the pool wait that long for a free connection too.
                timeout=httpx.Timeout(self._timeout, connect=10.0, read=read_timeout)
                if read_timeout is not None
                else httpx.USE_CLIENT_DEFAULT,
            )
            # Before the finally below releases anyone waiting on this endpoint.
            self._measure(bucket, response.headers)
        # Not connected, or no pooled connection free: the request never left.
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise BrainTransportError(
                f"{method} {path} could not connect: {exc}", maybe_delivered=False
            ) from exc
        except httpx.TimeoutException as exc:
            raise BrainTransportError(f"{method} {path} timed out", maybe_delivered=True) from exc
        except httpx.HTTPError as exc:
            raise BrainTransportError(
                f"{method} {path} failed: {exc}", maybe_delivered=True
            ) from exc
        finally:
            # Even a failed probe must release its endpoint, or every caller behind it waits
            # on a measurement that is never coming.
            self._measured[bucket].set()

        result = BrainResponse(
            status=response.status_code,
            headers=response.headers,
            # A refusal is always decoded, so error handling still reads its body.
            body=response.content if raw and response.status_code < 400 else _decode(response),
            retry_after=_parse_retry_after(response.headers),
            rate_limit=RateLimit.from_headers(response.headers),
            location=response.headers.get("location"),
        )
        if result.status == 429 and result.retry_after:
            # The server named its own wait: nobody sends before it is over.
            # Capped: every request waits on this, polls included, and a day-long Retry-After
            # would stop finished alphas being read.
            pause = min(result.retry_after, MAX_MEASURED_GAP)
            self._resume_at = max(self._resume_at, time.monotonic() + pause)

        if raise_for_status and response.status_code >= 400:
            raise self.to_error(method, path, result)
        return result

    def _persona_challenge(self, r: BrainResponse) -> tuple[str | None, str | None]:
        """The Persona inquiry in a 401, and the URL to open to complete it.

        A biometric step-up arrives as ``WWW-Authenticate: persona`` with the inquiry in a
        relative ``Location``; some responses put it in the body instead
        (``02-authentication.md``).
        """
        location = r.location
        if location:
            inquiry = parse_qs(urlsplit(location).query).get("inquiry", [""])[0]
            if inquiry:
                return inquiry, urljoin(self.base_url, location)

        inquiry = r.body.get("inquiry") if isinstance(r.body, dict) else None
        if isinstance(inquiry, str) and inquiry:
            return inquiry, None
        return None, None

    def to_error(self, method: str, path: str, r: BrainResponse) -> BrainError:
        """Map a failed response to a typed error (``docs/wqb-api/04-error-handling.md``)."""
        where = f"{method} {path}"
        body = r.body
        detail = body.get("detail") if isinstance(body, dict) else None

        if r.status == 401:
            inquiry, inquiry_url = self._persona_challenge(r)
            if inquiry:
                # Biometric step-up, not a credential failure (02-authentication.md).
                return BrainVerificationRequired(
                    "BRAIN requires identity verification before this session can be used.",
                    inquiry=inquiry,
                    url=inquiry_url,
                    body=body,
                )
            return BrainAuthError(
                f"{where}: not authenticated ({detail or 'session expired'})",
                status=401,
                body=body,
                detail=detail if isinstance(detail, str) else None,
            )

        if r.status == 403:
            return BrainForbidden(
                f"{where}: forbidden ({detail or 'account lacks permission for this request'})",
                status=403,
                body=body,
                detail=detail if isinstance(detail, str) else None,
            )

        if r.status == 404:
            return BrainNotFound(f"{where}: not found", status=404, body=body)

        if r.status == 400:
            fields = body if isinstance(body, dict) else {"detail": body}
            return BrainValidationError(
                f"{where}: rejected by the platform", fields=fields, body=body
            )

        if r.status == 429:
            if detail == DAILY_LIMIT_DETAIL:
                return BrainDailyLimitReached(
                    "Daily simulation limit reached. Please try tomorrow (EST time zone).",
                    body=body,
                )
            return BrainRateLimited(
                f"{where}: rate limited ({detail or 'too many requests'})",
                retry_after=r.retry_after,
                body=body,
            )

        if r.status == 503:
            return BrainServiceUnavailable(f"{where}: service unavailable", status=503, body=body)

        if r.status >= 500:
            return BrainServerError(f"{where}: server error {r.status}", status=r.status, body=body)

        return BrainError(f"{where}: unexpected status {r.status}", status=r.status, body=body)

    async def request_retrying(
        self,
        method: Method,
        path: str,
        *,
        attempts: int | None = None,
        base_backoff: float = 2.0,
        max_backoff: float = 60.0,
        **kwargs: Any,
    ) -> BrainResponse:
        """Issue a request, retrying ``429``, ``503``, other ``5xx`` and transport errors.

        Waits the server's ``Retry-After`` when it sends one, otherwise exponential backoff
        with jitter. The thresholds behind a ``429`` are server-side, so nothing here
        guesses a request rate: a ``429`` pauses every caller of this client for the wait.
        Never retries the daily cap.
        """
        attempts = attempts or self._default_attempts
        last: BrainError | None = None

        for attempt in range(1, attempts + 1):
            try:
                return await self.request(method, path, **kwargs)
            except BrainError as exc:
                if not exc.retryable or attempt == attempts:
                    raise
                last = exc

                delay = getattr(exc, "retry_after", None)
                if delay is None:
                    delay = min(base_backoff * (2 ** (attempt - 1)), max_backoff)
                delay = min(float(delay), max_backoff)
                # Jitter so parallel callers do not resynchronise on the same instant.
                delay += random.uniform(0, max(delay, 1.0) * 0.25)
                if isinstance(exc, BrainRateLimited):
                    self._resume_at = max(self._resume_at, time.monotonic() + delay)

                params = kwargs.get("params") or {}
                # Which scope is being throttled: without it a slow market and a starved one
                # look identical in the log.
                scope = "/".join(
                    str(params[k]) for k in ("region", "delay", "universe") if k in params
                )
                log.warning(
                    "brain.retrying",
                    path=path,
                    scope=scope or None,
                    attempt=attempt,
                    of=attempts,
                    delay=round(delay, 2),
                    reason=type(exc).__name__,
                )
                await asyncio.sleep(delay)

        if last is None:
            raise RuntimeError("retry loop ran zero attempts")
        raise last

    # -- the asynchronous job protocol -----------------------------------

    async def poll(self, path: str, *, version: str = DEFAULT_VERSION) -> BrainResponse:
        """Drive an asynchronous ``GET`` job to completion.

        Re-issues the request while the response carries ``Retry-After``, waiting what the
        server asks. Returns the first response *without* that header.
        """
        deadline = time.monotonic() + self._poll_timeout
        attempt = 0

        while True:
            # Retrying: a 429 or 503 between polls says nothing about the job.
            response = await self.request_retrying("GET", path, version=version)
            attempt += 1

            if not response.pending:
                return response

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrainPollTimeout(
                    f"{path} still pending after {attempt} polls; giving up. "
                    "The job may still complete server-side."
                )

            # The documented "poll now" (zero, past or unreadable) still waits a floor:
            # otherwise a job that stays pending re-requests as fast as the round trip.
            delay = min(max(response.retry_after or 0.0, MIN_POLL_DELAY), remaining)
            log.debug("brain.poll.waiting", path=path, attempt=attempt, delay=delay)
            await asyncio.sleep(delay)


def _decode(response: httpx.Response) -> Any:
    """Best-effort body decode. Some endpoints return empty bodies or non-JSON."""
    if not response.content:
        return None
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type:
        return response.text
    try:
        return response.json()
    except ValueError:
        return response.text
