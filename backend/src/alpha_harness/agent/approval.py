"""The server-side approval boundary for agent capabilities.

The application's existing confirmations live in React dialogs, which an agent calling
the service layer would bypass. This gate moves confirmation to the only place that can
run a capability: CONFIRM calls mint an in-memory proposal, and only a matching,
unexpired, unconsumed approval can cross the boundary. The store is intentionally
process-local, so the application must continue to run with a single uvicorn worker.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog
from pydantic import BaseModel, ValidationError

from ..config import REPO_ROOT
from .registry import (
    _GATE_TOKEN,
    LIMITS,
    AgentContext,
    Capability,
    CapabilityRegistry,
    Effect,
    GateBypass,
    Tier,
    UnknownCapability,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..state import AppState
    from ..tasks import ChangeHook

log = structlog.get_logger(__name__)

ORIGIN_RE: Final[re.Pattern[str]] = re.compile(
    r"^agent:thread=(?P<thread_id>\d+):call=(?P<call_id>[^\s:]+)$"
)
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
KILL_SWITCH_ENV: Final[str] = "AH_AGENT_ACTIONS_ENABLED"
_FALSEY: Final[frozenset[str]] = frozenset({"", "0", "false", "no", "off"})


def clip(text: Any, limit: int) -> str:
    """One line, at most ``limit`` characters, broken on a word."""
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[:limit].rsplit(" ", 1)[0] + "…"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    RUNNING = "running"
    EXECUTED = "executed"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"


_FINAL_STATUSES: Final[frozenset[ProposalStatus]] = frozenset(
    {
        ProposalStatus.APPROVED,
        ProposalStatus.EXECUTED,
        ProposalStatus.FAILED,
        ProposalStatus.REJECTED,
        ProposalStatus.EXPIRED,
        ProposalStatus.REVOKED,
    }
)


class ApprovalError(RuntimeError):
    status: int = 400
    code: str = "approval_error"

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra


class AgentActionsDisabled(ApprovalError):
    status, code = 403, "agent_actions_disabled"


class CapabilityBlocked(ApprovalError):
    status, code = 403, "capability_blocked"


class CapabilityArgumentError(ApprovalError):
    status, code = 422, "capability_arguments"


class ProposalNotFound(ApprovalError):
    status, code = 404, "proposal_not_found"


class ProposalExpired(ApprovalError):
    status, code = 410, "proposal_expired"


class ProposalAlreadyDecided(ApprovalError):
    status, code = 409, "proposal_already_decided"


class ProposalPayloadMismatch(ApprovalError):
    status, code = 409, "proposal_payload_mismatch"


class TooManyProposals(ApprovalError):
    status, code = 429, "too_many_proposals"


def canonical_payload(tool: str, arguments: Mapping[str, Any]) -> bytes:
    """Encode the exact capability and arguments covered by an approval."""
    body = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return b"alpha-harness/agent-approval/v1\n" + tool.encode() + b"\n" + body.encode("utf-8")


def payload_hash(tool: str, arguments: Mapping[str, Any]) -> str:
    """Return the domain-separated SHA-256 digest for an approval payload."""
    return hashlib.sha256(canonical_payload(tool, arguments)).hexdigest()


_runtime_actions_enabled: bool | None = None


@lru_cache(maxsize=1)
def _env_file_kill_switch() -> str | None:
    """Read the repo-root fallback once; process environment remains live."""
    try:
        lines = (REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines()
    except OSError, UnicodeError:
        return None

    found: str | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, raw_value = line.partition("=")
        if not separator or key.strip().casefold() != KILL_SWITCH_ENV.casefold():
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        found = value
    return found


def agent_actions_enabled() -> bool:
    """Resolve the live action switch without caching process-environment changes."""
    if _runtime_actions_enabled is not None:
        return _runtime_actions_enabled
    value = os.environ.get(KILL_SWITCH_ENV)
    if value is None:
        value = _env_file_kill_switch()
    return value is None or value.strip().casefold() not in _FALSEY


def set_agent_actions_enabled(value: bool | None) -> None:
    """Set or clear the process-local override used by the emergency kill switch."""
    global _runtime_actions_enabled
    _runtime_actions_enabled = value


@dataclass(slots=True)
class ActionProposal:
    id: str
    thread_id: int
    call_id: str
    tool: str
    label: str
    tier: Tier
    status: ProposalStatus
    summary: str
    detail: str
    effects: tuple[str, ...]
    arguments: dict[str, Any]
    payload_hash: str
    spends: dict[str, int]
    irreversible: bool
    origin: str
    context: AgentContext
    created_at: float
    created_mono: float
    expires_mono: float
    decided_at: float | None = None
    decided_mono: float | None = None
    result_summary: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        expires_at = self.created_at + (self.expires_mono - self.created_mono)
        error = None
        if self.error_message is not None:
            error = {
                "code": self.error_code or "proposal_error",
                "message": self.error_message,
            }
        result = None
        if self.result_summary is not None or error is not None:
            result = {"summary": self.result_summary or "", "error": error}
        return {
            "id": self.id,
            "threadId": self.thread_id,
            "callId": self.call_id,
            "tool": self.tool,
            "label": self.label,
            "tier": self.tier.value,
            "status": self.status.value,
            "summary": self.summary,
            "detail": self.detail,
            "effects": list(self.effects),
            "arguments": self.arguments,
            "payloadHash": self.payload_hash,
            "spends": {
                "brainSimulations": self.spends["brain_simulations"],
                "brainCalls": self.spends["brain_calls"],
                "correlationJobs": self.spends["correlation_jobs"],
                "llmRequests": self.spends["llm_requests"],
            },
            "irreversible": self.irreversible,
            "origin": self.origin,
            "createdAt": _iso_utc(self.created_at),
            "expiresAt": _iso_utc(expires_at),
            "expiresInSeconds": max(0.0, self.expires_mono - time.monotonic()),
            "result": result,
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class DispatchResult:
    status: Literal["executed", "needs_approval", "refused"]
    tool: str
    tier: Tier
    result: Any = None
    proposal: ActionProposal | None = None
    message: str | None = None
    instead: str | None = None
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tool": self.tool,
            "tier": self.tier.value,
            "result": self.result,
            "proposal": self.proposal.to_dict() if self.proposal is not None else None,
            "message": self.message,
            "instead": self.instead,
            "replayed": self.replayed,
        }


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def _summarise(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)[:400]
    return str(value)


def _declared_simulations(arguments: Mapping[str, Any]) -> int:
    simulations = arguments.get("simulations")
    if isinstance(simulations, list):
        return max(1, len(simulations))
    if isinstance(simulations, int) and not isinstance(simulations, bool):
        return max(1, simulations)
    return 1


class ApprovalGate:
    """Validate, propose, consume, and run capabilities through one boundary."""

    def __init__(
        self,
        state: AppState,
        capabilities: CapabilityRegistry,
        *,
        on_change: ChangeHook | None = None,
        ttl_seconds: float = LIMITS["proposal_ttl_seconds"],
        retention_seconds: float = LIMITS["proposal_retention_seconds"],
        max_pending: int = LIMITS["max_pending_proposals"],
        max_tracked: int = 256,
        execution_timeout: float = LIMITS["execution_timeout_seconds"],
    ) -> None:
        self._state = state
        self._caps = capabilities
        self._on_change = on_change
        self._ttl_seconds = ttl_seconds
        self._retention_seconds = retention_seconds
        self._max_pending = max_pending
        self._max_tracked = max_tracked
        self._execution_timeout = execution_timeout
        self._proposals: dict[str, ActionProposal] = {}

    async def dispatch(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        context: AgentContext,
        origin: str,
        thread_id: int | None = None,
        call_id: str | None = None,
    ) -> DispatchResult:
        """Apply a capability's tier and run only AUTO capabilities immediately."""
        cap = self._caps.get(tool)
        if cap is None:
            raise UnknownCapability(tool)
        if cap.tier is Tier.BLOCKED:
            log.warning("agent.blocked", tool=tool, origin=origin)
            raise CapabilityBlocked(f"{tool} is blocked: {cap.reason or cap.explains}")
        if cap.tier is Tier.HUMAN_ONLY:
            return DispatchResult(
                status="refused",
                tool=tool,
                tier=cap.tier,
                message=cap.reason,
                instead=cap.instead,
            )

        self._sweep()
        if not agent_actions_enabled():
            revoked = self._revoke_all_pending()
            log.warning("agent.kill_switch", tool=tool, revoked=revoked)
            if revoked or cap.tier is not Tier.AUTO:
                await self._notify()
            if cap.tier is not Tier.AUTO:
                raise AgentActionsDisabled("Agent actions are switched off.")
        if cap.params is None or not cap.is_callable():
            raise GateBypass(f"{tool} is declared {cap.tier} but reached the executing path.")

        params, canonical, digest = self._validate(cap, arguments)
        if cap.tier is Tier.AUTO:
            result = await self._execute(cap, params, context)
            return DispatchResult(status="executed", tool=tool, tier=cap.tier, result=result)
        if cap.tier is not Tier.CONFIRM:
            raise GateBypass(f"{tool} has unsupported tier {cap.tier}.")

        pending_count = sum(
            proposal.status is ProposalStatus.PENDING for proposal in self._proposals.values()
        )
        if pending_count >= self._max_pending:
            raise TooManyProposals("Too many proposals are awaiting a decision.")

        parsed_origin = ORIGIN_RE.fullmatch(origin)
        if parsed_origin is None and (thread_id is None or call_id is None):
            log.warning("agent.origin_unparsed", origin=origin, tool=tool)
        resolved_thread_id = thread_id
        if resolved_thread_id is None:
            resolved_thread_id = int(parsed_origin.group("thread_id")) if parsed_origin else 0
        resolved_call_id = call_id
        if resolved_call_id is None:
            resolved_call_id = parsed_origin.group("call_id") if parsed_origin else ""

        created_at = time.time()
        created_mono = time.monotonic()
        proposal = ActionProposal(
            id="prp_" + secrets.token_urlsafe(18),
            thread_id=resolved_thread_id,
            call_id=resolved_call_id,
            tool=cap.name,
            label=cap.confirm_label or cap.explains,
            tier=cap.tier,
            status=ProposalStatus.PENDING,
            summary=cap.explains,
            detail=cap.side_effect,
            effects=tuple(sorted(effect.value for effect in cap.effects)),
            arguments=canonical,
            payload_hash=digest,
            spends=self._spends(cap, canonical),
            irreversible=cap.irreversible,
            origin=origin,
            context=context,
            created_at=created_at,
            created_mono=created_mono,
            expires_mono=created_mono + self._ttl_seconds,
        )
        self._proposals[proposal.id] = proposal
        log.info("agent.proposal.created", id=proposal.id, tool=tool, hash=digest)
        await self._notify()
        return DispatchResult(
            status="needs_approval",
            tool=tool,
            tier=cap.tier,
            proposal=proposal,
        )

    async def approve(self, proposal_id: str, payload_hash: str) -> DispatchResult:
        """Consume one matching pending proposal, then run its stored arguments."""
        self._sweep()
        if not agent_actions_enabled():
            revoked = self._revoke_all_pending()
            log.warning("agent.kill_switch", proposal_id=proposal_id, revoked=revoked)
            await self._notify()
            raise AgentActionsDisabled("Agent actions are switched off.")

        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise ProposalNotFound(f"No proposal named {proposal_id!r}.")

        given = payload_hash.strip().casefold() if isinstance(payload_hash, str) else ""
        hash_matches = bool(_SHA256_RE.fullmatch(given)) and hmac.compare_digest(
            proposal.payload_hash, given
        )
        if not hash_matches:
            log.warning(
                "agent.proposal.mismatch",
                id=proposal.id,
                expected=proposal.payload_hash,
                got=given,
            )
            raise ProposalPayloadMismatch("The approved payload does not match the proposal.")

        if proposal.status is ProposalStatus.EXPIRED:
            raise ProposalExpired(f"Proposal {proposal.id} has expired.")
        if proposal.status is not ProposalStatus.PENDING:
            raise ProposalAlreadyDecided(
                f"Proposal {proposal.id} has already been consumed.",
                state=proposal.status.value,
            )

        cap = self._caps.get(proposal.tool)
        if cap is None:
            raise UnknownCapability(proposal.tool)
        if cap.tier is not Tier.CONFIRM or cap.params is None or not cap.is_callable():
            raise GateBypass(f"{proposal.tool} is no longer an executable CONFIRM capability.")

        try:
            params, _, actual_hash = self._validate(cap, proposal.arguments)
        except CapabilityArgumentError as exc:
            log.warning("agent.proposal.arguments_changed", id=proposal.id)
            raise ProposalPayloadMismatch(
                "The proposal's stored arguments no longer match its payload hash."
            ) from exc
        if not hmac.compare_digest(proposal.payload_hash, actual_hash):
            log.warning("agent.proposal.arguments_changed", id=proposal.id)
            raise ProposalPayloadMismatch(
                "The proposal's stored arguments no longer match its payload hash."
            )

        if time.monotonic() >= proposal.expires_mono:
            proposal.status = ProposalStatus.EXPIRED
            proposal.decided_at = time.time()
            proposal.decided_mono = time.monotonic()
            await self._notify()
            raise ProposalExpired(f"Proposal {proposal.id} has expired.")

        # Consumption happens before execution. If execution times out or the connection
        # drops after a side effect lands, a retry must fail instead of spending twice.
        proposal.status = ProposalStatus.RUNNING
        proposal.decided_at = time.time()
        proposal.decided_mono = time.monotonic()

        try:
            result = await self._execute(cap, params, proposal.context)
        except Exception as exc:  # noqa: BLE001 - a handler must not break the gate
            proposal.status = ProposalStatus.FAILED
            proposal.error_code = exc.code if isinstance(exc, ApprovalError) else type(exc).__name__
            proposal.error_message = clip(str(exc) or type(exc).__name__, 280)
            log.warning(
                "agent.proposal.failed",
                id=proposal.id,
                error=proposal.error_message,
            )
            outcome = DispatchResult(
                status="refused",
                tool=proposal.tool,
                tier=proposal.tier,
                message=proposal.error_message,
                proposal=proposal,
            )
        else:
            proposal.status = ProposalStatus.EXECUTED
            proposal.result_summary = clip(_summarise(result), 280)
            outcome = DispatchResult(
                status="executed",
                tool=proposal.tool,
                tier=proposal.tier,
                result=result,
                proposal=proposal,
            )

        proposal.decided_at = time.time()
        proposal.decided_mono = time.monotonic()
        await self._notify()
        return outcome

    async def reject(
        self,
        proposal_id: str,
        *,
        reason: str | None = None,
    ) -> ActionProposal:
        """Record a human rejection without running the capability."""
        self._sweep()
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise ProposalNotFound(f"No proposal named {proposal_id!r}.")
        if proposal.status is ProposalStatus.EXPIRED:
            raise ProposalExpired(f"Proposal {proposal.id} has expired.")
        if proposal.status is ProposalStatus.REJECTED:
            return proposal
        if proposal.status is not ProposalStatus.PENDING:
            raise ProposalAlreadyDecided(
                f"Proposal {proposal.id} has already been consumed.",
                state=proposal.status.value,
            )

        proposal.status = ProposalStatus.REJECTED
        proposal.decided_at = time.time()
        proposal.decided_mono = time.monotonic()
        proposal.error_message = reason
        log.info("agent.proposal.rejected", id=proposal.id, reason=reason)
        await self._notify()
        return proposal

    def pending(self) -> list[ActionProposal]:
        self._sweep()
        if not agent_actions_enabled():
            self._revoke_all_pending()
        return sorted(
            (
                proposal
                for proposal in self._proposals.values()
                if proposal.status is ProposalStatus.PENDING
            ),
            key=lambda proposal: proposal.created_mono,
        )

    def snapshot(self) -> dict[str, Any]:
        self._sweep()
        enabled = agent_actions_enabled()
        if not enabled:
            self._revoke_all_pending()
        proposals = sorted(
            (
                proposal.to_dict()
                for proposal in self._proposals.values()
                if proposal.status is ProposalStatus.PENDING
            ),
            key=lambda proposal: proposal["createdAt"],
        )
        return {"pending": proposals, "enabled": enabled}

    def _sweep(self) -> None:
        now_mono = time.monotonic()
        freshly_expired: set[str] = set()
        for proposal in self._proposals.values():
            if proposal.status is ProposalStatus.PENDING and proposal.expires_mono <= now_mono:
                proposal.status = ProposalStatus.EXPIRED
                proposal.decided_at = time.time()
                proposal.decided_mono = now_mono
                freshly_expired.add(proposal.id)
                log.info("agent.proposal.expired", id=proposal.id)

        for proposal_id, proposal in tuple(self._proposals.items()):
            if proposal_id in freshly_expired or proposal.status not in _FINAL_STATUSES:
                continue
            if (
                proposal.decided_mono is not None
                and now_mono - proposal.decided_mono > self._retention_seconds
            ):
                del self._proposals[proposal_id]

        while len(self._proposals) > self._max_tracked:
            candidates = [
                proposal
                for proposal in self._proposals.values()
                if proposal.id not in freshly_expired
                and proposal.status in _FINAL_STATUSES
                and proposal.decided_mono is not None
            ]
            if not candidates:
                break
            oldest = min(candidates, key=lambda proposal: proposal.decided_mono or 0.0)
            del self._proposals[oldest.id]

    def _spends(self, cap: Capability, canonical: Mapping[str, Any]) -> dict[str, int]:
        brain_effects = {
            Effect.BRAIN_READ,
            Effect.BRAIN_READ_THROTTLED,
            Effect.BRAIN_WRITE,
        }
        return {
            "brain_simulations": (
                _declared_simulations(canonical) if Effect.SIMULATION_QUOTA in cap.effects else 0
            ),
            "brain_calls": int(bool(cap.effects & brain_effects)),
            "correlation_jobs": int(Effect.BRAIN_READ_THROTTLED in cap.effects),
            "llm_requests": int(Effect.LLM_BUDGET in cap.effects),
        }

    def _validate(
        self,
        cap: Capability,
        arguments: Mapping[str, Any],
    ) -> tuple[BaseModel, dict[str, Any], str]:
        if cap.params is None:
            raise GateBypass(f"{cap.name} has no argument model.")
        try:
            params = cap.params.model_validate(arguments)
        except ValidationError as exc:
            problems = [str(problem) for problem in exc.errors(include_url=False)]
            raise CapabilityArgumentError(
                f"{cap.name}: invalid arguments",
                problems=problems,
            ) from exc

        try:
            dumped = params.model_dump(mode="json", by_alias=True)
            digest = payload_hash(cap.name, dumped)
            canonical = json.loads(canonical_payload(cap.name, dumped).split(b"\n", 2)[2])
        except (TypeError, ValueError) as exc:
            raise CapabilityArgumentError(
                f"{cap.name}: arguments are not canonical JSON",
                problems=[str(exc)],
            ) from exc

        try:
            round_trip = cap.params.model_validate(canonical).model_dump(
                mode="json",
                by_alias=True,
            )
            round_trip_hash = payload_hash(cap.name, round_trip)
        except (TypeError, ValueError, ValidationError) as exc:
            raise CapabilityArgumentError(
                f"{cap.name}: arguments do not round-trip",
                problems=[str(exc)],
            ) from exc
        if not hmac.compare_digest(round_trip_hash, digest):
            raise CapabilityArgumentError(
                f"{cap.name}: arguments do not round-trip",
                problems=["canonical validation changed the approved payload"],
            )
        return params, canonical, digest

    def _revoke_all_pending(self) -> int:
        now_wall = time.time()
        now_mono = time.monotonic()
        revoked = 0
        for proposal in self._proposals.values():
            if proposal.status is not ProposalStatus.PENDING:
                continue
            proposal.status = ProposalStatus.REVOKED
            proposal.decided_at = now_wall
            proposal.decided_mono = now_mono
            proposal.error_code = AgentActionsDisabled.code
            proposal.error_message = "Agent actions were switched off."
            revoked += 1
        return revoked

    async def _notify(self) -> None:
        if self._on_change is None:
            return
        result = self._on_change(self.snapshot())
        if asyncio.iscoroutine(result):
            await result

    async def _execute(
        self,
        cap: Capability,
        params: BaseModel,
        context: AgentContext,
    ) -> Any:
        return await asyncio.wait_for(
            cap.run(self._state, params, context, token=_GATE_TOKEN),
            timeout=self._execution_timeout,
        )
