"""What each lab stores in ``Study.sampler_params``, typed.

The column stays JSON, and existing rows were written before these models: keys keep the
spelling they were stored with (``datasetIds`` beside ``n_startup_trials``), unknown keys
survive a read and a write (``extra="allow"``), and every field a reader used to default
still defaults here. Only what a lab cannot run without is required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from ..db.models import Study


#: Studies with this sampler breed generations (labs.ga) instead of asking Optuna.
GA_SAMPLER = "ga"
#: Studies with this sampler write their own expressions (labs.search), asked define-by-run.
SEARCH_SAMPLER = "search"
TEMPLATE_SAMPLER = "template"
POWER_POOL_SAMPLER = "power-pool"
#: Studies that re-run one proven expression across markets and settings (tools.settings_sampler).
SETTINGS_SAMPLER = "settings-sampler"
#: Studies that re-shape one Alpha's expression at its own settings (tools.correlation_breaker).
CORRELATION_BREAKER = "correlation-breaker"
#: Studies that are research-lab tasks, run only from the Tasks tab, by their lab's name.
TASK_SAMPLERS = {
    SEARCH_SAMPLER: "Search Lab",
    TEMPLATE_SAMPLER: "Template Lab",
    GA_SAMPLER: "Evolution Lab",
    POWER_POOL_SAMPLER: "LLM Power Pool Lab",
    SETTINGS_SAMPLER: "Settings Sampler",
    CORRELATION_BREAKER: "Correlation Breaker",
}


class TaskParams(BaseModel):
    """Fields every research-lab task carries."""

    model_config = ConfigDict(extra="allow", serialize_by_alias=True)

    region: str
    delay: int
    #: Concurrent slots the task holds; its rounds are ten simulations per core.
    cores: int = 1
    #: When it was handed to the scheduler, which starts waiting tasks oldest first.
    queued_at: str | None = Field(
        default=None,
        validation_alias=AliasChoices("queuedAt", "queued_at"),
        serialization_alias="queuedAt",
    )
    #: Stopped early: it only scores what is already out, then completes.
    stopping: bool = False

    def dump(self) -> dict[str, Any]:
        """The JSON stored in the column, in its stored spelling."""
        return self.model_dump(exclude_none=True)


class SearchParams(TaskParams):
    space: dict[str, Any]
    decay: int = 0
    dataset_ids: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("datasetIds", "dataset_ids"),
        serialization_alias="datasetIds",
    )
    n_startup_trials: int = 20
    multivariate: bool = True
    group: bool = True


class TemplateParams(SearchParams):
    tree: dict[str, Any]


class EvolutionParams(TaskParams):
    universe: str
    seeds: list[str] = Field(default_factory=list)
    population: int = 100
    mutation_rate: float | None = Field(
        default=None,
        validation_alias=AliasChoices("mutationRate", "mutation_rate"),
        serialization_alias="mutationRate",
    )
    test_period: str | None = Field(
        default=None,
        validation_alias=AliasChoices("testPeriod", "test_period"),
        serialization_alias="testPeriod",
    )
    neutralizations: list[str] = Field(default_factory=list)


class PowerPoolParams(TaskParams):
    universes: list[str]
    neutralizations: list[str]
    universe: str | None = None
    dataset_ids: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("datasetIds", "dataset_ids"),
        serialization_alias="datasetIds",
    )
    model: str = ""
    #: Running LLM tallies: ``calls``, ``empty``, ``failed`` and ``byDataset``.
    llm: dict[str, Any] = Field(default_factory=dict)
    #: The last hundred LLM calls, for the task's detail view.
    calls: list[dict[str, Any]] = Field(default_factory=list)


class SettingsParams(TaskParams):
    """Settings Sampler: every simulation is written up front, so nothing is sampled.

    ``region`` and ``delay`` are the source Alpha's, shown on the task card; the task itself
    spans whichever markets were chosen.
    """

    alpha_id: str = Field(
        default="",
        validation_alias=AliasChoices("alphaId", "alpha_id"),
        serialization_alias="alphaId",
    )
    #: How many region/delay/universe markets the sweep covers, for the task's detail line.
    markets: int = 0
    #: Held at the source Alpha's values for every simulation in the sweep.
    decay: int = 0
    truncation: float = 0.08
    nan_handling: str = Field(
        default="ON",
        validation_alias=AliasChoices("nanHandling", "nan_handling"),
        serialization_alias="nanHandling",
    )


class BreakerParams(TaskParams):
    """Correlation Breaker: one Alpha re-shaped, every simulation written up front.

    The settings are the source Alpha's and are never varied, so they are recorded here to be
    shown on the task card rather than to be chosen from.
    """

    alpha_id: str = Field(
        default="",
        validation_alias=AliasChoices("alphaId", "alpha_id"),
        serialization_alias="alphaId",
    )
    universe: str = ""
    neutralization: str = ""
    decay: int = 0
    truncation: float = 0.08
    #: The recipes queued, by id, for the task's detail line.
    recipes: list[str] = Field(default_factory=list)


BY_SAMPLER: dict[str, type[TaskParams]] = {
    SEARCH_SAMPLER: SearchParams,
    TEMPLATE_SAMPLER: TemplateParams,
    GA_SAMPLER: EvolutionParams,
    POWER_POOL_SAMPLER: PowerPoolParams,
    SETTINGS_SAMPLER: SettingsParams,
    CORRELATION_BREAKER: BreakerParams,
}


def params_of[P: TaskParams](row: Study, kind: type[P]) -> P:
    """The row's stored parameters as ``kind``; raises if the row belongs to another lab."""
    expected = BY_SAMPLER.get(row.sampler)
    if expected is None or not issubclass(expected, kind):
        raise TypeError(f"A {row.sampler!r} task has no {kind.__name__}.")
    return kind.model_validate(row.sampler_params or {})


def task_params(row: Study) -> TaskParams:
    """The row's parameters as its own lab's model, whatever the lab."""
    return BY_SAMPLER[row.sampler].model_validate(row.sampler_params or {})
