"""Capability declarations shared by the agent catalogue and approval gate."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Container
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from ..catalog.queries import Tuple4
from ..schemas import Out

if TYPE_CHECKING:
    from ..state import AppState

log = structlog.get_logger(__name__)

type Dialect = Literal["openai", "gemini"]

LIMITS: Final[dict[str, int]] = {
    "max_tool_iterations": 6,
    "tool_calls_per_turn": 12,
    "brain_reads_per_turn": 8,
    "tool_result_chars": 4000,
    "turn_history_chars": 12000,
    "page_context_bytes": 8192,
    "proposal_ttl_seconds": 300,
    "proposal_retention_seconds": 900,
    "max_pending_proposals": 32,
    "max_enqueue_simulations": 50,
    "max_staged_tasks": 3,
    "execution_timeout_seconds": 120,
    "keepalive_seconds": 15,
}

_GATE_TOKEN: Final[object] = object()

_CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_SCHEMA_DROP_KEYS = frozenset({"title", "$schema", "examples", "discriminator"})
_SCHEMA_FORBIDDEN_KEYS = frozenset({"$ref", "$defs", "allOf", "patternProperties"})


class Tier(StrEnum):
    AUTO = "auto"
    CONFIRM = "confirm"
    HUMAN_ONLY = "human_only"
    BLOCKED = "blocked"


class Effect(StrEnum):
    LOCAL_READ = "local_read"
    LOCAL_WRITE = "local_write"
    LOCAL_DESTRUCTIVE = "local_destructive"
    BRAIN_READ = "brain_read"
    BRAIN_READ_THROTTLED = "brain_read_throttled"
    BRAIN_WRITE = "brain_write"
    SIMULATION_QUOTA = "simulation_quota"
    LLM_BUDGET = "llm_budget"
    BACKGROUND_JOB = "background_job"
    CREDENTIAL = "credential"
    SECRET_INPUT = "secret_input"


class Group(StrEnum):
    ORIENT = "orient"
    ALPHA = "alpha"
    PASS = "pass"
    CATALOG = "catalog"
    ACT = "act"


class CapabilityDeclarationError(ValueError):
    """A capability declaration violates the frozen registry contract."""


class UnknownCapability(LookupError):  # noqa: N818
    """The requested capability is not registered."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Unknown capability: {name}")


class MissingScopeError(ValueError):
    """Neither the call nor the current agent context supplied a scope."""


class GateBypass(RuntimeError):  # noqa: N818
    """Something tried to execute a capability without going through ApprovalGate."""


class Scope(Out):
    """The four-part market scope carried by the current screen."""

    instrument_type: str
    region: str
    delay: int
    universe: str


class Pick(Out):
    """A dataset or seed selection currently being carried between screens."""

    kind: str
    from_: str = Field(alias="from")
    count: int
    scope_label: str
    hazard: str


class Fact(Out):
    """A named scalar fact visible on the current screen."""

    key: str
    label: str
    value: str | int | float | bool | None
    caveat: str | None = None
    source: str


class UnknownField(Out):
    """Context the frontend could not establish without guessing."""

    field: str
    why: str


class AgentContext(Out):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    collected_at: str | None = None
    pathname: str = Field(default="", pattern=r"^(/[A-Za-z0-9/_\-.]{0,199})?$")
    area: str | None = None
    area_label: str | None = None
    tab: str | None = None
    params: dict[str, str] = Field(default_factory=dict)
    scope: Scope | None = None
    scope_key: Literal["data", "pool-submittable", "ai", "ai-chat"] | None = None
    alpha_id: str | None = None
    task_id: int | None = None
    field_id: str | None = None
    dataset_ids: tuple[str, ...] = ()
    template_id: int | None = None
    sync_run_id: int | None = None
    chat_thread_id: int | None = None
    lab: Literal["search", "template", "evolution", "power-pool"] | None = None
    lab_draft: dict[str, Any] | None = None
    pick: Pick | None = None
    thresholds: dict[str, float] | None = None
    thresholds_source: Literal["api-bars", "alpha-check"] | None = None
    facts: tuple[Fact, ...] = ()
    unknown: tuple[UnknownField, ...] = ()
    truncated: bool = False

    def require_scope(self, given: Scope | None) -> Tuple4:
        selected = given if given is not None else self.scope
        if selected is None:
            raise MissingScopeError("A market scope is required for this capability.")
        return Tuple4(
            instrument_type=selected.instrument_type,
            region=selected.region,
            delay=selected.delay,
            universe=selected.universe,
        )


type Handler = Callable[["AppState", BaseModel, AgentContext], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolError:
    """A provider-neutral error returned to the tool loop."""

    code: str
    message: str
    retryable: bool | None = None
    retry_after: float | None = None

    @classmethod
    def from_exception(cls, exc: Exception) -> ToolError:
        from ..api.deps import SIMPLE

        for error_type in type(exc).__mro__:
            mapped = SIMPLE.get(error_type)
            if mapped is None:
                continue
            _, code, message, retryable = mapped
            return cls(
                code=code,
                message=message or str(exc),
                retryable=retryable,
                retry_after=getattr(exc, "retry_after", None),
            )
        return cls(
            code="tool_error",
            message=str(exc),
            retryable=getattr(exc, "retryable", None),
            retry_after=getattr(exc, "retry_after", None),
        )


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    group: Group
    description: str
    explains: str
    side_effect: str
    tier: Tier
    effects: frozenset[Effect]
    params: type[BaseModel] | None
    example: dict[str, Any] | None
    method: str | None
    path: str | None
    conditional: bool
    irreversible: bool
    confirm_label: str | None
    max_result_chars: int
    produces_ids: tuple[str, ...]
    needs_ids: tuple[str, ...]
    fed_by: tuple[str, ...]
    reason: str | None
    instead: str | None
    _handler: Handler | None = field(repr=False, default=None)

    @property
    def spends_brain_quota(self) -> bool:
        return Effect.SIMULATION_QUOTA in self.effects

    @property
    def spends_llm_budget(self) -> bool:
        return Effect.LLM_BUDGET in self.effects

    @property
    def reads_brain(self) -> bool:
        return bool(self.effects & {Effect.BRAIN_READ, Effect.BRAIN_READ_THROTTLED})

    def is_callable(self) -> bool:
        return self._handler is not None

    def tool_schema(self, *, dialect: Dialect = "openai") -> dict[str, Any]:
        if self.params is None:
            raise CapabilityDeclarationError(
                f"{self.name}: a non-callable capability has no schema"
            )
        return {
            "name": self.name,
            "description": self.description,
            "parameters": json_schema(self.params, dialect=dialect),
        }

    async def run(
        self,
        state: AppState,
        params: BaseModel,
        context: AgentContext,
        *,
        token: object,
    ) -> Any:
        if token is not _GATE_TOKEN:
            log.error("agent.gate_bypass_attempt", capability=self.name)
            raise GateBypass(f"{self.name} may only be executed by ApprovalGate.")
        if self._handler is None:
            raise GateBypass(f"{self.name} is not callable ({self.tier}).")
        return await self._handler(state, params, context)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(self, cap: Capability) -> Capability:
        if cap.name in self._capabilities:
            raise CapabilityDeclarationError(f"Duplicate capability: {cap.name}")
        self._capabilities[cap.name] = cap
        return cap

    def get(self, name: str) -> Capability | None:
        return self._capabilities.get(name)

    def require(self, name: str) -> Capability:
        cap = self.get(name)
        if cap is None:
            raise UnknownCapability(name)
        return cap

    def all(self) -> tuple[Capability, ...]:
        return tuple(self._capabilities.values())

    def callable_names(self) -> tuple[str, ...]:
        return tuple(cap.name for cap in self.all() if cap.is_callable())

    def tier(self, name: str) -> Tier:
        return self.require(name).tier

    def declarations(self, *, route: str = "") -> list[ToolSchema]:
        caps = [
            cap for cap in self.all() if cap.tier in {Tier.AUTO, Tier.CONFIRM} and cap.is_callable()
        ]
        if route:
            caps.sort(key=lambda cap: not _route_matches(cap.path, route))
        return [ToolSchema(**cap.tool_schema()) for cap in caps]

    def tools_for_model(
        self,
        *,
        dialect: Dialect = "openai",
        only: Container[str] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            cap.tool_schema(dialect=dialect)
            for cap in self.all()
            if cap.tier in {Tier.AUTO, Tier.CONFIRM}
            and cap.is_callable()
            and (only is None or cap.name in only)
        ]

    def human_actions(self) -> tuple[Capability, ...]:
        return tuple(cap for cap in self.all() if cap.tier is Tier.HUMAN_ONLY)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": cap.name,
                "group": cap.group.value,
                "description": cap.description,
                "tier": cap.tier.value,
                "explains": cap.explains,
                "sideEffect": cap.side_effect,
                "effects": sorted(effect.value for effect in cap.effects),
                "conditional": cap.conditional,
                "irreversible": cap.irreversible,
                "producesIds": list(cap.produces_ids),
                "needsIds": list(cap.needs_ids),
                "fedBy": list(cap.fed_by),
                "reason": cap.reason,
                "instead": cap.instead,
            }
            for cap in self.all()
        ]

    def digest(self) -> str:
        rows = (
            "|".join(
                (
                    cap.name,
                    cap.tier.value,
                    ",".join(sorted(effect.value for effect in cap.effects)),
                    cap.method or "",
                    cap.path or "",
                )
            )
            for cap in sorted(self.all(), key=lambda item: item.name)
        )
        return hashlib.sha256("\n".join(rows).encode()).hexdigest()

    def validate(self) -> None:
        for cap in self.all():
            _validate_capability(cap)
        self._validate_pairings()

    def _validate_pairings(self) -> None:
        for cap in self.all():
            if cap.needs_ids and not cap.fed_by:
                _declaration_error(cap, "needs_ids requires at least one fed_by capability")

            produced: set[str] = set()
            for source_name in cap.fed_by:
                source = self.get(source_name)
                if source is None:
                    _declaration_error(cap, f"fed_by names unknown capability {source_name!r}")
                if not source.is_callable():
                    _declaration_error(cap, f"fed_by capability {source_name!r} is not callable")
                produced.update(source.produces_ids)

            missing = set(cap.needs_ids) - produced
            if missing:
                names = ", ".join(sorted(missing))
                _declaration_error(cap, f"fed_by capabilities do not produce: {names}")


def capability(
    *,
    name: str,
    group: Group,
    description: str,
    explains: str,
    side_effect: str,
    params: type[BaseModel] | None,
    tier: Tier,
    effects: frozenset[Effect],
    example: dict[str, Any] | None,
    method: str | None = None,
    path: str | None = None,
    conditional: bool = False,
    irreversible: bool = False,
    confirm_label: str | None = None,
    max_result_chars: int = LIMITS["tool_result_chars"],
    produces_ids: tuple[str, ...] = (),
    needs_ids: tuple[str, ...] = (),
    fed_by: tuple[str, ...] = (),
    registry: CapabilityRegistry | None = None,
) -> Callable[[Handler], Handler]:
    if not isinstance(tier, Tier):
        raise CapabilityDeclarationError(f"{name}: tier must be a Tier")

    def declare(handler: Handler) -> Handler:
        cap = Capability(
            name=name,
            group=group,
            description=description,
            explains=explains,
            side_effect=side_effect,
            tier=tier,
            effects=effects,
            params=params,
            example=example,
            method=method,
            path=path,
            conditional=conditional,
            irreversible=irreversible,
            confirm_label=confirm_label,
            max_result_chars=max_result_chars,
            produces_ids=produces_ids,
            needs_ids=needs_ids,
            fed_by=fed_by,
            reason=None,
            instead=None,
            _handler=handler,
        )
        if registry is not None:
            registry.register(cap)
        return handler

    return declare


def deny(
    *,
    name: str,
    group: Group,
    tier: Tier,
    reason: str,
    instead: str | None = None,
    description: str = "",
    explains: str,
    method: str | None = None,
    path: str | None = None,
    registry: CapabilityRegistry | None = None,
) -> Capability:
    if not isinstance(tier, Tier):
        raise CapabilityDeclarationError(f"{name}: tier must be a Tier")
    if tier not in {Tier.HUMAN_ONLY, Tier.BLOCKED}:
        raise CapabilityDeclarationError(f"{name}: deny() requires a non-callable tier")

    cap = Capability(
        name=name,
        group=group,
        description=description or explains,
        explains=explains,
        side_effect=reason,
        tier=tier,
        effects=frozenset(),
        params=None,
        example=None,
        method=method,
        path=path,
        conditional=False,
        irreversible=False,
        confirm_label=None,
        max_result_chars=LIMITS["tool_result_chars"],
        produces_ids=(),
        needs_ids=(),
        fed_by=(),
        reason=reason,
        instead=instead,
    )
    if registry is not None:
        registry.register(cap)
    return cap


def json_schema(
    model: type[BaseModel],
    *,
    dialect: Dialect = "openai",
) -> dict[str, Any]:
    if dialect not in {"openai", "gemini"}:
        raise CapabilityDeclarationError(f"Unsupported JSON Schema dialect: {dialect}")

    raw = model.model_json_schema(by_alias=True, ref_template="#/$defs/{model}")
    definitions = raw.get("$defs", {})
    schema = _inline_refs(raw, definitions, ())
    schema = _normalise_schema(schema)
    schema.pop("$defs", None)
    schema["type"] = "object"
    schema.setdefault("properties", {})
    schema["required"] = list(schema.get("required", []))

    if dialect == "openai":
        schema["additionalProperties"] = False
    else:
        schema = _for_gemini(schema)

    _lint_schema(schema, model.__name__)
    return schema


def _inline_refs(
    value: Any,
    definitions: dict[str, Any],
    stack: tuple[str, ...],
) -> Any:
    if isinstance(value, list):
        return [_inline_refs(item, definitions, stack) for item in value]
    if not isinstance(value, dict):
        return value

    reference = value.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            raise CapabilityDeclarationError(f"Unsupported JSON Schema reference: {reference!r}")
        if reference in stack:
            raise CapabilityDeclarationError(f"Recursive JSON Schema reference: {reference}")
        target = _resolve_ref(reference, definitions)
        merged = copy.deepcopy(target)
        merged.update({key: item for key, item in value.items() if key != "$ref"})
        return _inline_refs(merged, definitions, (*stack, reference))

    return {
        key: _inline_refs(item, definitions, stack) for key, item in value.items() if key != "$defs"
    }


def _resolve_ref(reference: str, definitions: dict[str, Any]) -> dict[str, Any]:
    current: Any = definitions
    for part in reference.removeprefix("#/$defs/").split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            raise CapabilityDeclarationError(f"Unknown JSON Schema reference: {reference}")
        current = current[key]
    if not isinstance(current, dict):
        raise CapabilityDeclarationError(f"JSON Schema reference is not an object: {reference}")
    return current


def _normalise_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalise_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalised = {
        key: _normalise_schema(item) for key, item in value.items() if key not in _SCHEMA_DROP_KEYS
    }
    variants = normalised.get("anyOf")
    if isinstance(variants, list) and len(variants) == 2:
        non_null = [item for item in variants if not _null_schema(item)]
        if len(non_null) == 1:
            collapsed = dict(non_null[0])
            collapsed.update({key: item for key, item in normalised.items() if key != "anyOf"})
            collapsed["nullable"] = True
            return collapsed
    return normalised


def _null_schema(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "null"


def _for_gemini(value: Any) -> Any:
    if isinstance(value, list):
        return [_for_gemini(item) for item in value]
    if not isinstance(value, dict):
        return value

    converted = {
        key: _for_gemini(item)
        for key, item in value.items()
        if key not in {"default", "additionalProperties"}
    }
    if "default" in value:
        encoded = json.dumps(value["default"], ensure_ascii=False, separators=(",", ":"))
        converted["description"] = f"{converted.get('description', '')} (default: {encoded})"
    return converted


def _lint_schema(schema: dict[str, Any], model_name: str) -> None:
    def visit(value: Any, depth: int, location: str) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, depth, f"{location}[{index}]")
            return
        if not isinstance(value, dict):
            return

        forbidden = _SCHEMA_FORBIDDEN_KEYS & value.keys()
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise CapabilityDeclarationError(f"{model_name}: {location} contains {names}")

        is_container = value.get("type") in {"object", "array"} or "properties" in value
        next_depth = depth + int(is_container)
        if next_depth > 4:
            raise CapabilityDeclarationError(f"{model_name}: {location} nests deeper than 3")

        properties = value.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                if not isinstance(child, dict) or not str(child.get("description", "")).strip():
                    raise CapabilityDeclarationError(
                        f"{model_name}: property {location}.{name} has no description"
                    )
                visit(child, next_depth, f"{location}.{name}")

        for key, child in value.items():
            if key != "properties":
                visit(child, next_depth, f"{location}.{key}")

    visit(schema, 0, "schema")


def _route_matches(path: str | None, route: str) -> bool:
    if path is None:
        return False
    pattern = re.sub(r"\{[^/{}]+\}", r"[^/]+", path)
    return re.fullmatch(pattern, route) is not None


def _validate_capability(cap: Capability) -> None:
    if not _CAPABILITY_NAME.fullmatch(cap.name):
        _declaration_error(cap, "name must match ^[a-z][a-z0-9_]{2,47}$")
    if not isinstance(cap.group, Group):
        _declaration_error(cap, "group must be a Group")
    if not isinstance(cap.tier, Tier):
        _declaration_error(cap, "tier must be a Tier")
    if not isinstance(cap.effects, frozenset) or any(
        not isinstance(effect, Effect) for effect in cap.effects
    ):
        _declaration_error(cap, "effects must be a frozenset of Effect values")
    if not cap.description.strip() or "\n" in cap.description or "\r" in cap.description:
        _declaration_error(cap, "description must be a non-empty single line")
    if len(cap.description) > 200:
        _declaration_error(cap, "description must be at most 200 characters")
    if not cap.explains.strip():
        _declaration_error(cap, "explains must be non-empty")
    if not cap.side_effect.strip():
        _declaration_error(cap, "side_effect must be non-empty")

    non_callable = cap.tier in {Tier.HUMAN_ONLY, Tier.BLOCKED}
    if (cap._handler is None) is not non_callable:
        _declaration_error(cap, "handler presence does not match tier")
    if (cap.reason is not None) is not non_callable:
        _declaration_error(cap, "reason presence does not match callability")
    if (cap.instead is not None) is not (cap.tier is Tier.HUMAN_ONLY):
        _declaration_error(cap, "instead must be set only for human-only capabilities")

    if cap.effects & {Effect.SIMULATION_QUOTA, Effect.BRAIN_WRITE} and cap.tier not in {
        Tier.CONFIRM,
        Tier.HUMAN_ONLY,
    }:
        _declaration_error(cap, "simulation quota and BRAIN writes require confirmation")
    if Effect.BRAIN_READ_THROTTLED in cap.effects and cap.tier not in {
        Tier.CONFIRM,
        Tier.HUMAN_ONLY,
    }:
        _declaration_error(cap, "throttled BRAIN reads require confirmation")
    if Effect.CREDENTIAL in cap.effects and cap.tier is not Tier.HUMAN_ONLY:
        _declaration_error(cap, "credential effects must be human-only")
    if Effect.SECRET_INPUT in cap.effects and cap.tier is not Tier.BLOCKED:
        _declaration_error(cap, "secret input effects must be blocked")
    if cap.tier is Tier.AUTO and not cap.effects <= {
        Effect.LOCAL_READ,
        Effect.LOCAL_WRITE,
        Effect.BRAIN_READ,
    }:
        _declaration_error(cap, "AUTO effects exceed the allowed set")
    if cap.conditional and cap.name != "get_alpha_detail":
        _declaration_error(cap, "only get_alpha_detail may be conditional")

    if cap.is_callable():
        if (
            cap.params is None
            or not isinstance(cap.params, type)
            or not issubclass(cap.params, BaseModel)
        ):
            _declaration_error(cap, "a callable capability requires a BaseModel params type")
        if cap.example is None:
            _declaration_error(cap, "a callable capability requires an example")
        try:
            cap.params.model_validate(cap.example)
        except ValidationError as exc:
            raise CapabilityDeclarationError(f"{cap.name}: example is invalid: {exc}") from exc
        json_schema(cap.params, dialect="openai")
        json_schema(cap.params, dialect="gemini")
    elif cap.params is not None or cap.example is not None:
        _declaration_error(cap, "a non-callable capability cannot have params or an example")

    if (cap.method is None) is not (cap.path is None):
        _declaration_error(cap, "method and path must be set together")
    if cap.method is not None and cap.method not in _METHODS:
        _declaration_error(cap, "method is not an allowed HTTP verb")
    if cap.path is not None and (
        not cap.path.startswith("/") or "?" in cap.path or "#" in cap.path
    ):
        _declaration_error(cap, "path must be an OpenAPI path template")


def _declaration_error(cap: Capability, message: str) -> None:
    raise CapabilityDeclarationError(f"{cap.name}: {message}")
