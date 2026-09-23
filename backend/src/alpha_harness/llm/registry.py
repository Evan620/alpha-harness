"""Which models exist, and what each one costs you.

Three limits apply at once on the AI Studio free tier, and they are not equally
important:

* **RPM** — requests per minute. Recovers in sixty seconds; briefly annoying.
* **TPM** — tokens per minute. Also recovers in sixty seconds.
* **RPD** — requests per day. **This is the one that ends your session.** It does not
  recover until midnight Pacific, and it varies by a factor of twenty-five across the
  roster: twenty a day on Gemini 3.8 Flash, five hundred on 3.5 Flash Lite.

That last point drives the design: every model carries its daily budget where it is chosen,
the Lite models are marked for bulk work, and someone who picks the newest model because it
sounds best does not get twenty questions and then silence.

**Nothing here is authoritative except as a starting point.** Google publishes no endpoint
for free-tier quotas, so the numbers below are transcribed and *will* drift; correcting one
is a one-line edit here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import structlog

from ..schemas import Out, camel_dict

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One model and its free-tier budget."""

    id: str
    label: str
    #: "text" for generation, "embedding", or "open" for the Gemma family.
    kind: str
    rpm: int
    tpm: int
    rpd: int
    #: Why you would pick this one. Shown where the model is chosen.
    summary: str = ""
    #: True for a model with a daily budget large enough to work in.
    bulk: bool = False
    recommended: bool = False
    #: Set when the model came from the API rather than the built-in table, meaning its
    #: limits are guesses rather than transcribed.
    discovered: bool = False
    #: Whose key answers for this model. A key only ever serves its own provider.
    provider: str = "google"

    def to_dict(self) -> dict[str, Any]:
        return camel_dict(self)


class LLMModel(Out):
    """:class:`ModelInfo` on the wire."""

    id: str
    label: str
    kind: Literal["text", "embedding", "open"]
    rpm: int
    tpm: int
    rpd: int
    summary: str
    #: Room for volume work.
    bulk: bool
    recommended: bool
    #: Limits are guessed, not published.
    discovered: bool
    provider: str


class ModelDefaults(Out):
    chat: str
    deep: str


class LLMModels(Out):
    models: list[LLMModel]
    defaults: ModelDefaults
    note: str


#: Transcribed from the AI Studio free-tier rate limits. Expect to correct these.
BUILTIN: tuple[ModelInfo, ...] = (
    ModelInfo(
        "gemini-3.8-flash",
        "Gemini 3.8 Flash",
        "text",
        rpm=5,
        tpm=250_000,
        rpd=20,
        summary=(
            "The strongest reasoning on the free tier. Twenty requests a day — spend "
            "them on hard questions, not on browsing."
        ),
        recommended=True,
    ),
    ModelInfo(
        "gemini-3.7-flash",
        "Gemini 3.7 Flash",
        "text",
        rpm=5,
        tpm=250_000,
        rpd=20,
        summary="Previous generation, same twenty-a-day budget.",
    ),
    ModelInfo(
        "gemini-3.6-flash",
        "Gemini 3.6 Flash",
        "text",
        rpm=5,
        tpm=250_000,
        rpd=20,
        summary="Previous generation, same twenty-a-day budget.",
    ),
    ModelInfo(
        "gemini-3.5-flash",
        "Gemini 3.5 Flash",
        "text",
        rpm=5,
        tpm=250_000,
        rpd=20,
        summary="Previous generation, same twenty-a-day budget.",
    ),
    ModelInfo(
        "gemini-3.5-flash-lite",
        "Gemini 3.5 Flash Lite",
        "text",
        rpm=15,
        tpm=250_000,
        rpd=500,
        summary=(
            "Five hundred requests a day — twenty-five times the budget of a full Flash "
            "model. The one to use for anything repetitive."
        ),
        bulk=True,
        recommended=True,
    ),
    ModelInfo(
        "gemini-3.1-flash-lite",
        "Gemini 3.1 Flash Lite",
        "text",
        rpm=15,
        tpm=250_000,
        rpd=500,
        summary="The same generous daily budget, one generation back.",
        bulk=True,
    ),
    ModelInfo(
        "gemini-3-flash",
        "Gemini 3 Flash",
        "text",
        rpm=5,
        tpm=250_000,
        rpd=20,
        summary="Twenty requests a day.",
    ),
    ModelInfo(
        "gemini-2.5-flash",
        "Gemini 2.5 Flash",
        "text",
        rpm=5,
        tpm=250_000,
        rpd=20,
        summary="Older generation. Twenty requests a day.",
    ),
    ModelInfo(
        "gemini-2.5-flash-lite",
        "Gemini 2.5 Flash Lite",
        "text",
        rpm=10,
        tpm=250_000,
        rpd=20,
        summary=(
            "Lite in speed but not in budget — still only twenty a day, unlike the 3.1 "
            "and 3.5 Lite models."
        ),
    ),
    ModelInfo(
        "gemma-4-31b",
        "Gemma 4 31B",
        "open",
        rpm=30,
        tpm=16_000,
        rpd=14_400,
        summary=(
            "Open-weight, and effectively unlimited by daily count. The 16K token-per-"
            "minute ceiling is the real constraint — too small for the full dataset tree."
        ),
        bulk=True,
    ),
    ModelInfo(
        "gemma-4-26b",
        "Gemma 4 26B",
        "open",
        rpm=30,
        tpm=16_000,
        rpd=14_400,
        summary="As above, slightly smaller.",
        bulk=True,
    ),
    ModelInfo(
        "gemini-embedding-2",
        "Gemini Embedding 2",
        "embedding",
        rpm=100,
        tpm=30_000,
        rpd=1_000,
        summary="For similarity search over descriptions, not for answering questions.",
    ),
    ModelInfo(
        "gemini-embedding-1",
        "Gemini Embedding 1",
        "embedding",
        rpm=100,
        tpm=30_000,
        rpd=1_000,
        summary="Previous embedding generation.",
    ),
)

#: For a model discovered from the API with no published limits. The *most* restrictive real
#: row, so an unknown model cannot silently burn a day's quota.
UNKNOWN_LIMITS = {"rpm": 5, "tpm": 250_000, "rpd": 20}

#: For a paid provider, where the person's own spend limit is the only real ceiling. A daily
#: request budget of 0 already means "no cap" to :meth:`Ledger.headroom`; per-minute limits
#: cannot use 0, which would read as "none allowed", so they take this instead.
NO_LIMIT = 1_000_000_000

#: Every LLM-powered feature here runs on GLM unless the person picks another model: it is
#: the provider this operator subscribes to, and the only one with a key. Flash for bulk work
#: (the Power Pool lab writes many alphas per run), the full model for one hard question.
DEFAULT_MODEL = "glm-5.3-flash"
#: For a single hard question where quality matters more than the daily budget.
DEEP_MODEL = "glm-5.3"


class ModelRegistry:
    """The model roster: the table above, plus whatever the API turns out to offer."""

    def __init__(self) -> None:
        self._models: dict[str, ModelInfo] = {m.id: m for m in BUILTIN}
        # Imported late: providers describes itself in terms of ModelInfo, so importing
        # it at module level would be a cycle.
        from .providers import provider_models

        for model in provider_models():
            self._models.setdefault(model.id, model)

    # -- reading ---------------------------------------------------------

    def __contains__(self, model_id: str) -> bool:
        return model_id in self._models

    def get(self, model_id: str) -> ModelInfo | None:
        return self._models.get(model_id)

    def require(self, model_id: str) -> ModelInfo:
        info = self._models.get(model_id)
        if info is None:
            raise KeyError(
                f"{model_id!r} is not a known model. Choose one of: "
                + ", ".join(sorted(self._models))
            )
        return info

    def all(self, kind: str | None = None, provider: str | None = None) -> list[ModelInfo]:
        """Every model, richest daily budget first — because that is what runs out."""
        models = [
            m
            for m in self._models.values()
            if (kind is None or m.kind == kind) and (provider is None or m.provider == provider)
        ]
        return sorted(models, key=lambda m: (-m.rpd, -m.rpm, m.id))

    def to_dict(self, providers: set[str] | None = None) -> dict[str, Any]:
        """The roster. ``providers`` narrows it to the ones a Key can actually answer for,
        so a chooser never offers a model whose first use would be an error."""
        models = [m for m in self.all() if providers is None or m.provider in providers]
        return {
            "models": [m.to_dict() for m in models],
            "defaults": {"chat": DEFAULT_MODEL, "deep": DEEP_MODEL},
            "note": (
                "Requests per day is the limit that ends a session — it does not reset "
                "until midnight Pacific, and it varies twenty-five-fold across these "
                "models. Adding a second API key doubles it."
            ),
        }

    # -- editing ---------------------------------------------------------

    def merge_discovered(self, names: list[str], provider: str = "google") -> list[str]:
        """Add models the API reports that we have never heard of.

        Their limits are unknown, so they get the most restrictive real budget and are
        flagged ``discovered`` rather than passing a guess off as a measurement.
        """
        added: list[str] = []
        for raw in names:
            model_id = raw.removeprefix("models/")
            if model_id in self._models:
                continue
            kind = (
                "embedding"
                if "embedding" in model_id
                else "open"
                if "gemma" in model_id
                else "text"
            )
            # A paid account is billed per token and publishes no daily request cap, so
            # UNKNOWN_LIMITS (a free tier's twenty a day) would invent a ceiling that stops
            # work the person has already paid for. Their own spend limit is the real one.
            from .providers import get as provider_spec

            paid = provider_spec(provider).paid
            self._models[model_id] = ModelInfo(
                id=model_id,
                label=model_id.replace("-", " ").title(),
                kind=kind,
                summary=(
                    "Reported by the API on a paid account, so no daily cap is imposed here."
                    if paid
                    else "Reported by the API. Its free-tier limits are unknown, so a "
                    "conservative budget is assumed until you correct it."
                ),
                discovered=True,
                provider=provider,
                rpm=NO_LIMIT if paid else UNKNOWN_LIMITS["rpm"],
                tpm=NO_LIMIT if paid else UNKNOWN_LIMITS["tpm"],
                rpd=0 if paid else UNKNOWN_LIMITS["rpd"],
            )
            added.append(model_id)
        if added:
            log.info("llm.registry.discovered", models=added)
        return added
