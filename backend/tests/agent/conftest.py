"""Published fixtures for every agent test lane.

Two details are load-bearing: ``LifespanManager.app`` is its wrapped ASGI callable,
not the FastAPI object whose state we inspect; and the ordinary harness stops the
optimizer, engine, and tracker because live loops made the probe suite flaky 2 in 5
and poisoned teardown when the BaseException sentinel fired in the background.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import BaseModel, ConfigDict, Field

from alpha_harness.agent.approval import ApprovalGate, set_agent_actions_enabled
from alpha_harness.agent.registry import (
    LIMITS,
    AgentContext,
    CapabilityRegistry,
    Effect,
    Group,
    Tier,
    capability,
    deny,
)
from alpha_harness.brain.client import BrainClient as RealBrainClient
from alpha_harness.brain.schemas import (
    Alpha,
    AlphaCode,
    Check,
    SampleStats,
    SimulationRequest,
    SimulationSettings,
    SimulationType,
)
from alpha_harness.config import Settings
from alpha_harness.db.models import SimStatus, SimulationRecord, Study
from alpha_harness.engine.dedup import hash_payload

from .fixtures import NEAR_MISS
from .harness import (
    Harness,
    OutboundBrainCall,
    RecordingTransport,
    ScriptedProvider,
    assert_nothing_spent,
    snapshot,
)

__all__ = [
    "CALLS",
    "Harness",
    "OutboundBrainCall",
    "RecordingTransport",
    "ScriptedProvider",
    "assert_nothing_spent",
    "reset_calls",
    "snapshot",
]


class FixtureParams(BaseModel):
    """Small but real capability argument model used by approval-boundary tests."""

    model_config = ConfigDict(extra="forbid")

    value: int = Field(default=1, description="A value captured by the fixture handler.")
    label: str = Field(default="fixture", description="A label captured with the value.")


CALLS: list[tuple[str, dict[str, Any], AgentContext]] = []


def reset_calls() -> None:
    CALLS.clear()


@pytest.fixture(autouse=True)
def _reset_kill_switch(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    set_agent_actions_enabled(None)
    monkeypatch.delenv("AH_AGENT_ACTIONS_ENABLED", raising=False)
    try:
        yield
    finally:
        set_agent_actions_enabled(None)


@pytest.fixture
def brain_transport() -> RecordingTransport:
    return RecordingTransport()


@asynccontextmanager
async def _running_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    brain_transport: RecordingTransport,
    *,
    keep_engine_running: bool,
) -> AsyncIterator[Harness]:
    import alpha_harness.state as state_module
    from alpha_harness.main import create_app

    def client_factory(*args: Any, **kwargs: Any) -> RealBrainClient:
        kwargs["transport"] = brain_transport.as_httpx()
        return RealBrainClient(*args, **kwargs)

    monkeypatch.setattr(state_module, "BrainClient", client_factory)
    settings = Settings(
        data_dir=tmp_path / "ah",
        min_request_interval_seconds=0.0,
        request_attempts=1,
        poll_timeout_seconds=5.0,
        min_retry_after_seconds=0.01,
    )
    app = create_app(settings)

    async with LifespanManager(app, startup_timeout=60.0) as manager:
        state = app.state.harness
        await state.optimizer.stop()
        if not keep_engine_running:
            await state.engine.stop()
        await state.tracker.stop()
        assert brain_transport.calls == [], (
            "application startup attempted a BRAIN call; the offline harness fails here first"
        )

        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Harness(app=app, client=client, state=state, brain=brain_transport)


@pytest.fixture
async def harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    brain_transport: RecordingTransport,
) -> AsyncIterator[Harness]:
    async with _running_harness(
        tmp_path,
        monkeypatch,
        brain_transport,
        keep_engine_running=False,
    ) as running:
        yield running


@pytest.fixture
async def live_engine_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    brain_transport: RecordingTransport,
) -> AsyncIterator[Harness]:
    async with _running_harness(
        tmp_path,
        monkeypatch,
        brain_transport,
        keep_engine_running=True,
    ) as running:
        yield running


@pytest.fixture
def gate_registry() -> Iterator[CapabilityRegistry]:
    """Build four real declarations without constructing the application state."""
    reset_calls()
    registry = CapabilityRegistry()

    @capability(
        name="fake_auto",
        group=Group.ACT,
        description="Execute an offline automatic fixture action.",
        explains="Exercises the automatic branch of the approval gate.",
        side_effect="Records one in-memory fixture call.",
        params=FixtureParams,
        tier=Tier.AUTO,
        effects=frozenset({Effect.LOCAL_WRITE}),
        example={"value": 1, "label": "fixture"},
        registry=registry,
    )
    async def fake_auto(
        _state: Any,
        params: BaseModel,
        context: AgentContext,
    ) -> dict[str, Any]:
        arguments = params.model_dump(mode="json", by_alias=True)
        CALLS.append(("fake_auto", arguments, context))
        return {"called": "fake_auto", "arguments": arguments}

    @capability(
        name="fake_confirm",
        group=Group.ACT,
        description="Propose an offline confirmation fixture action.",
        explains="Exercises the confirmed branch of the approval gate.",
        side_effect="Records one in-memory fixture call after approval.",
        params=FixtureParams,
        tier=Tier.CONFIRM,
        effects=frozenset({Effect.LOCAL_WRITE}),
        example={"value": 2, "label": "fixture"},
        confirm_label="Run fixture action",
        registry=registry,
    )
    async def fake_confirm(
        _state: Any,
        params: BaseModel,
        context: AgentContext,
    ) -> dict[str, Any]:
        arguments = params.model_dump(mode="json", by_alias=True)
        CALLS.append(("fake_confirm", arguments, context))
        return {"called": "fake_confirm", "arguments": arguments}

    # Non-callable declarations intentionally have params=None and no handler.
    deny(
        name="fake_human_only",
        group=Group.ACT,
        tier=Tier.HUMAN_ONLY,
        reason="This fixture action requires a person.",
        instead="Perform the fixture action in its screen.",
        explains="Exercises a human-only refusal.",
        registry=registry,
    )
    deny(
        name="fake_blocked",
        group=Group.ACT,
        tier=Tier.BLOCKED,
        reason="This fixture action is blocked.",
        explains="Exercises a blocked refusal.",
        registry=registry,
    )
    registry.validate()
    try:
        yield registry
    finally:
        reset_calls()


@pytest.fixture
def gate_state() -> SimpleNamespace:
    """A state stand-in proving ApprovalGate itself does not need a live AppState."""
    return SimpleNamespace(kind="offline-approval-fixture")


@pytest.fixture
def gate(gate_state: SimpleNamespace, gate_registry: CapabilityRegistry) -> ApprovalGate:
    return ApprovalGate(
        gate_state,
        gate_registry,
        ttl_seconds=LIMITS["proposal_ttl_seconds"],
        retention_seconds=LIMITS["proposal_retention_seconds"],
        execution_timeout=LIMITS["execution_timeout_seconds"],
    )


@pytest.fixture
def catalog_registry() -> CapabilityRegistry:
    from alpha_harness.agent.catalog import REGISTRY

    return REGISTRY


@pytest.fixture
def agent_context() -> AgentContext:
    return AgentContext(pathname="/pool/submittable")


@pytest.fixture
def seed_alpha(harness: Harness) -> Callable[..., Awaitable[str]]:
    async def seed(
        record: Mapping[str, Any] | Alpha | None = None,
        **overrides: Any,
    ) -> str:
        if isinstance(record, Alpha):
            alpha = record.model_copy(update=overrides)
        elif record is not None and "id" in record:
            wire = copy.deepcopy(dict(record))
            wire.update(overrides)
            alpha = Alpha.model_validate(wire)
        else:
            values = copy.deepcopy(NEAR_MISS)
            if record is not None:
                values.update(record)
            values.update(overrides)
            settings = SimulationSettings(
                instrument_type=values.get("instrument_type", "EQUITY"),
                region=values["region"],
                universe=values["universe"],
                delay=values["delay"],
                neutralization=values["neutralization"],
                decay=values["decay"],
                truncation=values["truncation"],
            )
            alpha = Alpha(
                id=values["alpha_id"],
                type=values.get("type", SimulationType.REGULAR),
                settings=settings,
                regular=AlphaCode(
                    code=values["expression"],
                    operator_count=values.get("operator_count", 2),
                ),
                date_created=values.get("date_created", "2026-09-19T08:00:00Z"),
                name=values.get("name"),
                status=values.get("status", "UNSUBMITTED"),
                in_sample=SampleStats(
                    sharpe=values["sharpe"],
                    fitness=values["fitness"],
                    turnover=values["turnover"],
                    checks=[Check.model_validate(check) for check in values["checks"]],
                ),
            )
        await harness.state.alphas.save_alphas([alpha])
        return alpha.id

    return seed


@pytest.fixture
def seed_task(harness: Harness) -> Callable[..., Awaitable[int]]:
    async def seed(
        *,
        name: str | None = None,
        template_source: str = "name: Fixture\nexpression: rank(close)\n",
        **values: Any,
    ) -> int:
        row = Study(
            name=name or f"fixture-study-{uuid.uuid4().hex}",
            template_source=template_source,
            **values,
        )
        async with harness.state.db.session() as session:
            session.add(row)
            await session.flush()
        return row.id

    return seed


@pytest.fixture
def seed_queued_simulation(harness: Harness) -> Callable[..., Awaitable[int]]:
    async def seed(
        request: SimulationRequest | Mapping[str, Any] | None = None,
        *,
        expression: str = "rank(ts_delta(close, 5))",
        region: str = "USA",
        delay: int = 1,
        universe: str = "TOP3000",
        task: str = "fixture",
    ) -> int:
        if request is None:
            parsed = SimulationRequest(
                settings=SimulationSettings(region=region, delay=delay, universe=universe),
                regular=expression,
            )
        else:
            parsed = SimulationRequest.model_validate(request)
        payload = parsed.to_wire()
        settings = parsed.settings
        row = SimulationRecord(
            request_hash=hash_payload(payload),
            payload=payload,
            expression=parsed.regular or parsed.combo or parsed.selection,
            sim_type=str(parsed.type),
            instrument_type=settings.instrument_type,
            region=settings.region,
            delay=settings.delay,
            language=settings.language,
            universe=settings.universe,
            task=task,
            status=SimStatus.QUEUED,
        )
        async with harness.state.db.session() as session:
            session.add(row)
            await session.flush()
        return row.id

    return seed


@pytest.fixture
def spend(harness: Harness) -> Callable[[], Awaitable[dict[str, Any]]]:
    return partial(snapshot, harness.state, harness.brain)


@pytest.fixture
def scripted_provider() -> Callable[[Sequence[Any]], ScriptedProvider]:
    return ScriptedProvider
