"""Green gates for the F-0 substrate and the agent modules already shipped today."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from alpha_harness.agent.approval import ApprovalGate, CapabilityBlocked
from alpha_harness.agent.registry import AgentContext, CapabilityRegistry, GateBypass, Tier

from .conftest import CALLS, reset_calls
from .harness import (
    Harness,
    OutboundBrainCall,
    RecordingTransport,
    assert_nothing_spent,
    grep,
    snapshot,
    source_files,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = BACKEND_ROOT / "src" / "alpha_harness" / "agent"
TEST_ROOT = BACKEND_ROOT / "tests"


def test_outbound_sentinel_survives_except_exception() -> None:
    assert issubclass(OutboundBrainCall, BaseException)
    assert not issubclass(OutboundBrainCall, Exception)


async def test_recording_transport_records_before_it_denies() -> None:
    transport = RecordingTransport()
    assert isinstance(transport, httpx.AsyncBaseTransport)

    with pytest.raises(OutboundBrainCall) as raised:
        async with httpx.AsyncClient(
            transport=transport.as_httpx(),
            base_url="https://api.worldquantbrain.com",
        ) as client:
            await client.post("/simulations", json={"fixture": True})

    assert transport.calls == [("POST", "https://api.worldquantbrain.com/simulations")]
    assert raised.value.method == "POST"
    assert raised.value.url == "https://api.worldquantbrain.com/simulations"


async def test_recording_transport_only_serves_explicit_routes() -> None:
    transport = RecordingTransport()
    transport.route("GET", "/operators", httpx.Response(200, json={"results": []}))

    async with httpx.AsyncClient(
        transport=transport.as_httpx(),
        base_url="https://api.worldquantbrain.com",
    ) as client:
        response = await client.get("/operators?limit=10")

    assert response.json() == {"results": []}
    assert transport.calls == [("GET", "https://api.worldquantbrain.com/operators?limit=10")]
    transport.reset()
    assert transport.calls == []


def test_gate_registry_models_callability_explicitly(
    gate_registry: CapabilityRegistry,
) -> None:
    callable_caps = [cap for cap in gate_registry.all() if cap.is_callable()]
    denied_caps = [cap for cap in gate_registry.all() if not cap.is_callable()]

    assert {cap.tier for cap in callable_caps} == {Tier.AUTO, Tier.CONFIRM}
    assert all(
        cap.params is not None and issubclass(cap.params, BaseModel) for cap in callable_caps
    )
    assert {cap.tier for cap in denied_caps} == {Tier.HUMAN_ONLY, Tier.BLOCKED}
    assert all(cap.params is None for cap in denied_caps)


async def test_gate_executes_a_callable_without_live_app_state(
    gate: ApprovalGate,
    agent_context: AgentContext,
) -> None:
    reset_calls()
    result = await gate.dispatch(
        "fake_auto",
        {"value": 7, "label": "offline"},
        context=agent_context,
        origin="test",
    )

    assert result.status == "executed"
    assert [("fake_auto", {"value": 7, "label": "offline"}, agent_context)] == CALLS


async def test_non_callable_entries_never_reach_a_handler(
    gate: ApprovalGate,
    agent_context: AgentContext,
) -> None:
    reset_calls()
    refused = await gate.dispatch("fake_human_only", {}, context=agent_context, origin="test")
    assert refused.status == "refused"
    assert refused.instead == "Perform the fixture action in its screen."

    with pytest.raises(CapabilityBlocked):
        await gate.dispatch("fake_blocked", {}, context=agent_context, origin="test")
    assert CALLS == []


async def test_capability_run_rejects_a_non_gate_token(
    gate_registry: CapabilityRegistry,
    agent_context: AgentContext,
) -> None:
    cap = gate_registry.require("fake_auto")
    assert cap.params is not None
    params = cap.params.model_validate(cap.example)

    with pytest.raises(GateBypass):
        await cap.run(object(), params, agent_context, token=object())
    assert CALLS == []


async def test_nothing_spent_covers_two_engine_ticks(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = await snapshot(harness.state, harness.brain)
    original_tick = harness.state.engine.tick
    tick_count = 0

    async def counted_tick() -> int:
        nonlocal tick_count
        tick_count += 1
        return await original_tick()

    monkeypatch.setattr(harness.state.engine, "tick", counted_tick)
    with pytest.raises(ValueError, match="at least two"):
        await assert_nothing_spent(harness, before, ticks=1)
    await assert_nothing_spent(harness, before)

    assert tick_count == 2
    assert harness.brain.calls == []


def test_agent_package_contract_surface_is_import_safe() -> None:
    init_path = AGENT_ROOT / "__init__.py"
    assert init_path.read_text(encoding="utf-8") == (
        '"""The agent surface: what the assistant is allowed to do, declared once."""\n'
    )
    for forbidden in ("pages.py", "remedies.py", "loop.py", "approvals.py", "context.py"):
        assert not (AGENT_ROOT / forbidden).exists()


def test_gate_token_and_handler_are_confined_to_the_gate_modules() -> None:
    files = source_files(AGENT_ROOT)
    token_files = {path.name for path, _, _ in grep(r"\b_GATE_TOKEN\b", files)}
    handler_files = {path.name for path, _, _ in grep(r"\._handler\b", files)}
    assert token_files == {"registry.py", "approval.py"}
    assert handler_files == {"registry.py"}

    forbidden_patterns = (
        r"def invoke\(",
        r"def execute\(",
        r"\.invoke\(",
        r"(?:registry|capabilities|caps)\.execute\(",
        r"cap\.call\(",
    )
    hits = [hit for pattern in forbidden_patterns for hit in grep(pattern, files)]
    assert hits == []


def test_no_tier_is_compared_to_a_bare_string() -> None:
    files = source_files(AGENT_ROOT)
    pattern = re.compile(
        r"(?:==\s*['\"](?:auto|confirm|human_only|blocked)['\"]"
        r"|in\s*\(\s*['\"](?:auto|confirm|human_only|blocked)['\"]\s*,)"
    )
    assert grep(pattern, files) == []


def test_agent_limits_are_declared_once() -> None:
    files = source_files(AGENT_ROOT)
    names = (
        "MAX_TOOL_ITERATIONS",
        "PROPOSAL_TTL_SECONDS",
        "PROPOSAL_RETENTION_SECONDS",
        "MAX_PENDING_PROPOSALS",
        "TOOL_RESULT_CHARS",
        "KEEPALIVE_SECONDS",
        "EXECUTION_TIMEOUT_SECONDS",
    )
    name_hits = [hit for name in names for hit in grep(rf"\b{re.escape(name)}\s*=", files)]
    assert name_hits == []

    registry_path = AGENT_ROOT / "registry.py"
    registry_lines = registry_path.read_text(encoding="utf-8").splitlines()
    limits_start = next(
        index for index, line in enumerate(registry_lines, 1) if line.startswith("LIMITS: Final")
    )
    limits_end = next(
        index
        for index, line in enumerate(registry_lines[limits_start:], limits_start + 1)
        if line == "}"
    )
    literal_hits = grep(r"(?<!\d)(?:300|900|4000|12000|8192)(?!\d)", files)
    outside_limits = [
        hit
        for hit in literal_hits
        if hit[0] != registry_path or not limits_start <= hit[1] <= limits_end
    ]
    assert outside_limits == []


def test_no_test_may_skip_or_xfail() -> None:
    files = [path for path in source_files(TEST_ROOT) if path != Path(__file__).resolve()]
    forbidden = r"\b(?:pytest\.skip|pytest\.mark\.skip|skipif|xfail|importorskip)\b"
    assert grep(forbidden, files) == []
