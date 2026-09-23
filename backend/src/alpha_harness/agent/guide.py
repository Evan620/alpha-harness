"""What each page is for, in words a new consultant can act on.

The agent's picture of the platform. `explain_page` returns one entry; the system prompt
carries the one-line map of all of them so the agent can point people to the right page.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Page:
    route: str
    title: str
    summary: str
    detail: str
    actions: tuple[str, ...]


PAGES: tuple[Page, ...] = (
    Page(
        "/dashboard",
        "Dashboard",
        "Today at a glance: getting-started checklist, the day plan and the submittable count.",
        "Getting Started ticks off setup from real state (signed in, catalog synced, vault synced, "
        "LLM key added) and hides once done. Below it, the day plan suggests what to run next, "
        "Osmosis-aware, and the Submittable Alphas tile counts alphas that pass every check for "
        "ONE scope (default USA · D1 · TOP3000), not globally.",
        ("GET /api/today", "GET /api/plan/pm", "POST /api/plan/suggest", "POST /api/plan/start"),
    ),
    Page(
        "/matrix",
        "Simulation Matrix",
        "Every simulation in flight or recently finished, with the batch engine's queue and quota.",
        "The engine batches up to 10 expressions per BRAIN multi-simulation and ticks every 2 s. "
        "Active and recent runs, the daily simulation allowance and the queue live here. "
        "Cancelling or dropping the queue stops work that has not reached BRAIN yet.",
        ("GET /api/simulations/active", "GET /api/simulations/quota", "POST /api/simulations"),
    ),
    Page(
        "/data",
        "Data Explorer",
        "Every data field in a market, filterable, with coverage and how often it is used.",
        "Pick a scope (region · delay · universe), then browse datasets and fields. A field opens "
        "its detail: type, coverage, alpha count and availability across regions. Fields with high "
        "coverage and a low alpha count are the uncrowded ones. The Market tab shows the sync "
        "state.",
        (
            "POST /api/catalog/fields",
            "GET /api/catalog/datasets",
            "GET /api/catalog/fields/{field_id}",
        ),
    ),
    Page(
        "/labs",
        "Research Labs",
        "The four generators that write alphas for you: Search, Template, Evolution, LLM Power "
        "Pool.",
        "Each lab is configured on its own page and adds a task to Tasks, where it actually runs. "
        "Search writes one- and two-operator alphas from chosen datasets; Template fills a block "
        "template; Evolution breeds seed alphas; Power Pool asks the LLM for Power-Pool-sized "
        "alphas.",
        ("GET /api/labs",),
    ),
    Page(
        "/labs/search",
        "Search Lab",
        "Choose datasets, cores and a simulation budget; it writes simple alphas steering to "
        "Sharpe.",
        "Adds a search task to Tasks. It generates one- and two-operator expressions from the "
        "chosen datasets' fields and spends real BRAIN simulations when the task runs.",
        (
            "GET /api/search-lab/options",
            "POST /api/search-lab/preview",
            "POST /api/search-lab/tasks",
        ),
    ),
    Page(
        "/labs/template",
        "Template Lab",
        "Build an expression template from blocks, then sweep its slots with fields and operators.",
        "A Scratch-style builder: drag operator blocks onto slots or click a slot to pick. Preview "
        "shows how many expressions the template expands to; adding it creates a task in Tasks.",
        (
            "GET /api/template-lab/templates",
            "POST /api/template-lab/preview",
            "POST /api/template-lab/tasks",
        ),
    ),
    Page(
        "/labs/evolution",
        "Evolution Lab",
        "Breed seed alphas into children, scored on the first 8 years so the last 2 stay "
        "out-of-sample.",
        "Pick seeds (or auto-seed), cores and simulations, then add the breeding to Tasks.",
        (
            "POST /api/evolution-lab/seeds/auto",
            "POST /api/evolution-lab/preview",
            "POST /api/evolution-lab/tasks",
        ),
    ),
    Page(
        "/labs/power-pool",
        "LLM Power Pool Lab",
        "An LLM writes Power Pool alphas (≤8 operators, ≤3 fields) for chosen datasets.",
        "Power Pool alphas get waivers on LOW_FITNESS/LOW_SHARPE when simple enough. The task "
        "spends LLM budget and BRAIN simulations while it runs in Tasks.",
        ("GET /api/power-pool-lab/options", "POST /api/power-pool-lab/tasks"),
    ),
    Page(
        "/tools",
        "Tools",
        "Single-purpose helpers: Settings Sampler, Submission Planner, Correlation Breaker.",
        "Settings Sampler sweeps one alpha across simulation settings to find where it works best. "
        "Submission Planner picks which alphas to submit and in what order (it only plans; the "
        "person submits on BRAIN). Correlation Breaker re-shapes an alpha already in the "
        "Production Pool so a new version is less correlated.",
        ("GET /api/tools/settings-sampler/options", "POST /api/tools/submission-planner/plan"),
    ),
    Page(
        "/tools/settings-sampler",
        "Settings Sampler",
        "Sweep one alpha across simulation settings (neutralization, decay, truncation...).",
        "Preview shows the settings grid; adding it creates a task in Tasks that spends "
        "simulations.",
        ("POST /api/tools/settings-sampler/preview", "POST /api/tools/settings-sampler/tasks"),
    ),
    Page(
        "/tools/submission-planner",
        "Submission Planner",
        "Choose which submittable alphas to submit and in what order, to maximise score.",
        "Plans from a task's submittable alphas. Marking one submitted only records it locally; "
        "the real submission is always done by the person on BRAIN.",
        ("POST /api/tools/submission-planner/plan", "POST /api/tools/submission-planner/submitted"),
    ),
    Page(
        "/tools/correlation-breaker",
        "Correlation Breaker",
        "Re-shape an alpha already in the Production Pool into less-correlated variants.",
        "Preview the variants, then add a task that simulates them.",
        (
            "POST /api/tools/correlation-breaker/preview",
            "POST /api/tools/correlation-breaker/tasks",
        ),
    ),
    Page(
        "/tasks",
        "Tasks",
        "Everything the labs and tools added. Only here does a task run, pause or stop; a row "
        "opens its results.",
        "Each row is a lab task with its progress and best result. Run, pause, stop, change or "
        "remove a task; open its top alphas.",
        (
            "GET /api/lab-tasks",
            "POST /api/lab-tasks/{task_id}/run",
            "GET /api/lab-tasks/{task_id}/top",
        ),
    ),
    Page(
        "/pool",
        "Alphas",
        "Your alphas: Stored (the local vault of everything simulated) and Submittable.",
        "Stored lists every alpha synced from BRAIN, searchable and filterable. Submittable shows "
        "alphas that pass the platform's checks for a scope, plus near-misses one fix away. An "
        "alpha opens its detail: PnL, settings, submission checks and correlations on request. "
        "Submitting to BRAIN is never done by the agent.",
        (
            "POST /api/alphas/search",
            "GET /api/vault/submittable",
            "GET /api/alphas/{alpha_id}/check",
        ),
    ),
    Page(
        "/alpha",
        "Alpha",
        "One alpha in full: will it submit, how it earned its PnL, where it came from.",
        "The verdict (submission checks against their limits) leads; below it PnL vs "
        "investability-constrained PnL, drawdown, yearly Sharpe, correlations and properties. "
        "Properties (name, category, colour, tags, description) save to BRAIN but never submit. "
        "A region-agnostic alpha is one of a family of four judged together.",
        (
            "GET /api/alphas/{alpha_id}",
            "GET /api/alphas/{alpha_id}/check",
            "PATCH /api/alphas/{alpha_id}",
        ),
    ),
    Page(
        "/portfolio",
        "Portfolio",
        "Your submitted alphas combined at equal weight, the way BRAIN combines its own pool.",
        "Shows combined performance and Power Pool performance, a PnL chart with train/test, "
        "drawdown, and a correlation heatmap: red pairs move together and add little, green "
        "pairs diversify. BRAIN refuses a Power Pool pair above 0.5 correlation.",
        ("POST /api/portfolio/sync", "POST /api/portfolio/compute"),
    ),
    Page(
        "/ai",
        "LLM Integration",
        "LLM providers, keys, daily budget, prompts and the data-picking assistant.",
        "Keys are added and removed by the human only. Budget shows remaining requests per model "
        "today. The Assistant tab suggests real data fields for an idea.",
        ("GET /api/llm/keys", "GET /api/llm/models", "POST /api/chat"),
    ),
    Page(
        "/pyramids",
        "Sync with BRAIN",
        "Download every market's data fields, and see the pyramid multipliers you are earning.",
        "The sync matrix downloads data fields per market. The pyramid map shows BRAIN's "
        "multiplier"
        "for each Region · Delay · Category, with a tick where 3+ of your alphas count this "
        "quarter."
        "Higher-multiplier scopes pay more for the same alpha.",
        ("POST /api/catalog/sync", "GET /api/catalog/pyramids", "POST /api/vault/sync"),
    ),
)


def page_for(route: str) -> Page | None:
    """The most specific page whose route prefixes `route`."""
    route = route or "/"
    best: Page | None = None
    for page in PAGES:
        matches = route == page.route or route.startswith(page.route + "/")
        if matches and (best is None or len(page.route) > len(best.route)):
            best = page
    return best


def site_map() -> str:
    def shown(route: str) -> str:
        return "/alpha/<alphaId> (only with a real id)" if route == "/alpha" else route

    return "\n".join(f"- {shown(p.route)}: {p.title}. {p.summary}" for p in PAGES)


def explain(route: str) -> dict[str, object]:
    page = page_for(route)
    if page is None:
        return {"route": route, "known": False, "siteMap": site_map()}
    return {
        "route": page.route,
        "title": page.title,
        "summary": page.summary,
        "detail": page.detail,
        "keyActions": list(page.actions),
    }
