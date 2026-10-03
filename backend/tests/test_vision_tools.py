"""Vision can use everything the harness can do, and each tool is classified by what it spends.

The catalog is derived from the OpenAPI document, so a new route is a new tool. These tests
pin the two ways that goes wrong: a route that never reaches the catalog, and a spending
list that names routes which no longer exist (which silently stops metering).
"""

from __future__ import annotations

import re

from alpha_harness.agent import actions
from alpha_harness.agent.registry import Effect, Tier
from alpha_harness.main import create_app

#: Vision's own controls (goal, permissions, model, sessions) and the health probe stay out:
#: the agent must not be able to change its own settings.
EXCLUDED = re.compile(r"^/api/(agent(/.*)?|health)$")
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _operations() -> set[tuple[str, str]]:
    paths = create_app().openapi()["paths"]
    return {
        (method.upper(), path)
        for path, ops in paths.items()
        for method in ops
        if method.upper() in METHODS and path.startswith("/api/") and not EXCLUDED.match(path)
    }


def test_every_harness_route_is_a_vision_tool() -> None:
    _registry, index = actions.build(create_app())
    tools = {(e["method"], e["path"]) for e in index.values()}
    missing = sorted(_operations() - tools)
    assert not missing, f"routes Vision cannot call: {missing}"


def test_llm_spending_list_names_only_routes_that_exist() -> None:
    llm_routes = {path for _method, path in _operations() if actions.SPENDS_LLM.search(path)}
    # Every alternative in the pattern must match a real route, or metering is silently off.
    alternatives = re.findall(r"[a-z][a-z0-9/-]*", actions.SPENDS_LLM.pattern.split("(", 1)[1])
    for alt in alternatives:
        assert any(path.endswith(alt) for path in llm_routes), f"stale LLM route: {alt}"


def test_classification_of_llm_and_preview_routes() -> None:
    tier, effects = actions._classify("POST", "/api/chat")
    assert Effect.LLM_BUDGET in effects and tier is Tier.CONFIRM
    _tier, effects = actions._classify("POST", "/api/power-pool-lab/tasks")
    assert Effect.LLM_BUDGET in effects
    # The Power Pool preview calls nothing and simulates nothing: free to read.
    tier, effects = actions._classify("POST", "/api/power-pool-lab/preview")
    assert tier is Tier.AUTO and Effect.LLM_BUDGET not in effects
    # Research rounds are local memory writes: no approval card.
    tier, _effects = actions._classify("POST", "/api/research/rounds")
    assert tier is Tier.AUTO


def test_correlation_queue_status_is_a_free_read() -> None:
    tier, effects = actions._classify("GET", "/api/correlations/queue")
    assert tier is Tier.AUTO and Effect.BRAIN_READ_THROTTLED not in effects
    # Real correlation reads still spend the slot and need approval.
    tier, effects = actions._classify("GET", "/api/alphas/{alpha_id}/correlations/{kind}")
    assert tier is Tier.CONFIRM and Effect.BRAIN_READ_THROTTLED in effects
    tier, effects = actions._classify("GET", "/api/alphas/{alpha_id}/check")
    assert Effect.BRAIN_READ_THROTTLED in effects
