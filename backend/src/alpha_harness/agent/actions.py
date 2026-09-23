"""Every UI action as a capability, derived from the app's own routes.

The UI does nothing the HTTP API cannot, so one capability per API operation gives the agent
exactly the reach a person has, and no more. Each capability's handler calls its route
in-process through ASGI, so the agent goes through the same validation, errors and side
effects as a button press. Nothing executes except through :class:`ApprovalGate`.

Tiering is conservative by construction: a GET or a known read-only POST (previews,
searches) is AUTO; anything else asks. Credentials and keys are human-only.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from fastapi import FastAPI

from .registry import (
    AgentContext,
    Capability,
    CapabilityRegistry,
    Effect,
    Group,
    Handler,
    Tier,
    capability,
    deny,
)

RESULT_CHARS = 6_000

#: POSTs that only read: previews, searches and option resolution.
READ_ONLY_POSTS = re.compile(
    r"(/preview$|^/api/alphas/search$|^/api/catalog/(fields|facets|coverage-matrix)$"
    r"|^/api/auth/settings-options$|^/api/vault/alphas/(query|k-ratio)$"
    r"|^/api/vault/mix/(candidates|correlations)$|^/api/harvest/fields$|^/api/analyse/sql$)"
)
#: Local writes that spend nothing and are worth having happen freely. A memory that costs
#: an approval prompt to write does not get written, and then the agent repeats itself.
AUTO_POSTS = re.compile(r"^/api/journal$")

#: Previews that still spend something are not read-only.
SPENDING_PREVIEWS = re.compile(r"^/api/power-pool-lab/preview$")
SPENDS_SIMS = re.compile(
    r"(/run$|/run-all$|/tasks$|/quick$|^/api/simulations(/queue)?$|/start$|/advance$|^/api/plan/lucky$)"
)
SPENDS_LLM = re.compile(
    r"^/api/(llm/(run|advise|power-pool|power-pool/queue|explain)|plan/advise|chat)$"
)

HUMAN_ONLY = {
    ("POST", "/api/auth/login"): "Signing in to BRAIN is yours: it can open a biometric check.",
    ("POST", "/api/auth/logout"): "Signing out ends the BRAIN session for everything running.",
    ("POST", "/api/auth/verify"): "Identity verification is a biometric check only you can do.",
    ("POST", "/api/update"): "Updating the app restarts it; that is yours to decide.",
    ("POST", "/api/quit"): "Quitting the app stops everything running.",
    ("DELETE", "/api/auth/credential"): "Forgetting the stored login is yours to decide.",
    ("PUT", "/api/llm/keys/{key_id}"): "Enabling or disabling an LLM key is yours.",
    ("DELETE", "/api/llm/keys/{key_id}"): "Removing an LLM key is yours.",
}
BLOCKED = {
    (
        "POST",
        "/api/llm/keys",
    ): "An API key is a secret; paste it into LLM Integration → Keys yourself.",
}
SKIP = {"/api/health"}


class HttpArgs(BaseModel):
    """Arguments for one API call, exactly as the UI would send them."""

    model_config = ConfigDict(extra="forbid")

    path: dict[str, str] = Field(default_factory=dict, description="Path parameters")
    query: dict[str, str | int | float | bool] = Field(
        default_factory=dict, description="Query-string parameters"
    )
    body: dict[str, Any] | list[Any] | None = Field(default=None, description="JSON body")


def _name(path: str, method: str, taken: set[str]) -> str:
    base = path.removeprefix("/api/").replace("{", "").replace("}", "")
    base = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    name = f"{method.lower()}_{base}"[:47].rstrip("_")
    stem, n = name, 2
    while name in taken:
        suffix = f"_{n}"
        name = stem[: 47 - len(suffix)] + suffix
        n += 1
    taken.add(name)
    return name


#: GETs that start a BRAIN job (correlation is one job per account) or re-run checks.
THROTTLED_GETS = re.compile(r"(/correlations(/[^/]+)?$|^/api/alphas/\{alpha_id\}/check$)")


def _classify(method: str, path: str) -> tuple[Tier, frozenset[Effect]]:
    if method == "GET" and THROTTLED_GETS.search(path):
        return Tier.CONFIRM, frozenset({Effect.BRAIN_READ_THROTTLED, Effect.LOCAL_WRITE})
    if method == "GET":
        return Tier.AUTO, frozenset({Effect.LOCAL_READ, Effect.LOCAL_WRITE, Effect.BRAIN_READ})
    if method == "POST" and READ_ONLY_POSTS.search(path) and not SPENDING_PREVIEWS.search(path):
        return Tier.AUTO, frozenset({Effect.LOCAL_READ, Effect.BRAIN_READ})
    if method == "POST" and AUTO_POSTS.search(path):
        return Tier.AUTO, frozenset({Effect.LOCAL_WRITE})
    effects = {Effect.LOCAL_WRITE}
    if method == "DELETE":
        effects.add(Effect.LOCAL_DESTRUCTIVE)
    if SPENDS_SIMS.search(path):
        effects |= {Effect.SIMULATION_QUOTA, Effect.BACKGROUND_JOB}
    if SPENDS_LLM.search(path) or SPENDING_PREVIEWS.search(path):
        effects.add(Effect.LLM_BUDGET)
    if method == "PATCH" and path.startswith("/api/alphas/"):
        effects.add(Effect.BRAIN_WRITE)
    return Tier.CONFIRM, frozenset(effects)


def _handler(app: FastAPI, method: str, template: str) -> Handler:
    async def call(_state: Any, params: BaseModel, _context: AgentContext) -> Any:
        # The registry hands every handler the same three arguments; this one needs
        # only the parsed params, and its shape is guaranteed by the gate's validation.
        if not isinstance(params, HttpArgs):  # pragma: no cover - the gate validates
            raise TypeError(f"{template} received {type(params).__name__}")
        url = template
        for key, value in params.path.items():
            url = url.replace("{" + key + "}", httpx.URL(path=str(value)).raw_path.decode())
        if "{" in url:
            missing = re.findall(r"{(\w+)}", url)
            return {"ok": False, "status": 400, "error": f"missing path parameters: {missing}"}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            headers={"x-harness-client": "1"},
            timeout=120.0,
        ) as client:
            response = await client.request(
                method,
                url,
                params={
                    k: str(v).lower() if isinstance(v, bool) else v for k, v in params.query.items()
                },
                json=params.body if method != "GET" else None,
            )
        try:
            data: Any = response.json()
        except ValueError:
            data = response.text
        text = json.dumps(data, default=str)
        return {
            "ok": response.status_code < 400,
            "status": response.status_code,
            "truncated": len(text) > RESULT_CHARS,
            "data": data if len(text) <= RESULT_CHARS else text[:RESULT_CHARS],
        }

    return call


def _group(path: str) -> Group:
    if path.startswith(("/api/alphas", "/api/vault")):
        return Group.ALPHA
    if path.startswith("/api/catalog"):
        return Group.CATALOG
    if path.startswith(("/api/today", "/api/plan", "/api/tasks", "/api/labs")):
        return Group.ORIENT
    return Group.ACT


def build(app: FastAPI) -> tuple[CapabilityRegistry, dict[str, dict[str, Any]]]:
    """A registry with one capability per API operation, plus a searchable index."""
    registry = CapabilityRegistry()
    index: dict[str, dict[str, Any]] = {}
    taken: set[str] = set()
    for path, ops in app.openapi().get("paths", {}).items():
        if not path.startswith("/api/") or path in SKIP or path.startswith("/api/agent"):
            continue
        for method_lower, op in sorted(ops.items()):
            method = method_lower.upper()
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            name = _name(path, method, taken)
            key = (method, path)
            summary = (op.get("summary") or name.replace("_", " ")).strip()
            description = f"{method} {path} — {summary}"[:200]
            explains = ((op.get("description") or summary).strip().splitlines() or [summary])[0][
                :400
            ]
            tags = list(op.get("tags") or [])
            if key in BLOCKED or key in HUMAN_ONLY:
                blocked = key in BLOCKED
                cap = deny(
                    name=name,
                    group=_group(path),
                    tier=Tier.BLOCKED if blocked else Tier.HUMAN_ONLY,
                    reason=(BLOCKED if blocked else HUMAN_ONLY)[key],
                    instead=None if blocked else "Ask the person to do this in the UI.",
                    explains=explains,
                    method=method,
                    path=path,
                    registry=registry,
                )
            else:
                tier, effects = _classify(method, path)
                capability(
                    name=name,
                    group=_group(path),
                    description=description,
                    explains=explains,
                    side_effect="none" if tier is Tier.AUTO else ", ".join(sorted(effects)),
                    params=HttpArgs,
                    tier=tier,
                    effects=effects,
                    example={"path": {}, "query": {}, "body": None},
                    method=method,
                    path=path,
                    irreversible=method == "DELETE",
                    confirm_label=f"{method} {path}",
                    registry=registry,
                )(_handler(app, method, path))
                cap = registry.require(name)
            index[name] = _entry(cap, path, summary, tags)
    return registry, index


def _entry(cap: Capability, path: str, summary: str, tags: list[str]) -> dict[str, Any]:
    return {
        "name": cap.name,
        "method": cap.method,
        "path": cap.path,
        "summary": summary,
        "tier": cap.tier.value,
        "effects": sorted(e.value for e in cap.effects),
        "pathParams": re.findall(r"{(\w+)}", path),
        "tags": tags,
    }


def search(index: dict[str, dict[str, Any]], query: str, limit: int = 25) -> list[dict[str, Any]]:
    words = [w for w in re.split(r"\W+", query.lower()) if w]
    if not words:
        return list(index.values())[:limit]
    scored = []
    for entry in index.values():
        hay = (
            f"{entry['name']} {entry['path']} {entry['summary']} {' '.join(entry['tags'])}".lower()
        )
        score = sum(hay.count(w) for w in words)
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda s: -s[0])
    return [e for _, e in scored[:limit]]


def request_schema(app: FastAPI, method: str, path: str) -> dict[str, Any]:
    """Query parameters and body schema for one operation, $refs resolved one level deep."""
    spec = app.openapi()
    op = spec.get("paths", {}).get(path, {}).get(method.lower(), {})
    schemas = spec.get("components", {}).get("schemas", {})

    def resolve(node: Any, depth: int = 0) -> Any:
        if depth > 4:
            return node
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                return resolve(schemas.get(ref.rsplit("/", 1)[-1], {}), depth + 1)
            return {k: resolve(v, depth) for k, v in node.items() if k not in {"title", "examples"}}
        if isinstance(node, list):
            return [resolve(v, depth) for v in node]
        return node

    body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get(
        "schema"
    )
    params = [
        {
            "name": p.get("name"),
            "in": p.get("in"),
            "required": p.get("required", False),
            "schema": resolve(p.get("schema")),
        }
        for p in op.get("parameters", [])
    ]
    out = json.loads(json.dumps({"parameters": params, "body": resolve(body)}, default=str))
    text = json.dumps(out)
    if len(text) > 8_000:
        return {"parameters": params, "body": text[:8_000], "truncated": True}
    return out
