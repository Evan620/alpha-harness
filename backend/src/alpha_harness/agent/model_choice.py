"""Which model Vision thinks with: chosen by the person in the UI (``/model``), never by Vision.

A pick is either a model already on the roster, or a provider plus a model slug the person
pasted, such as NVIDIA's ``deepseek-ai/deepseek-v4.1-flash``. A pasted slug is checked against
the provider's own model list before it is accepted, so a typo cannot become a model that
fails every turn. It then joins the roster with a modest budget, shown where it is chosen,
because a free provider publishes no limits for it and a guess must not spend a day.

The endpoint lives under ``/api/agent``, which is outside Vision's action catalog, so the
agent cannot switch its own model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from ..llm.registry import NO_LIMIT, ModelInfo, ModelRegistry

if TYPE_CHECKING:
    from pathlib import Path

log = structlog.get_logger(__name__)

#: For a pasted slug on a free provider. One Vision goal is 20-60 calls, so the registry's
#: twenty-a-day guess for discovered models would end a run before it started.
CUSTOM_LIMITS = {"rpm": 20, "tpm": 1_000_000, "rpd": 500}


@dataclass(frozen=True)
class CustomModel:
    id: str
    provider: str


class ModelChoice:
    def __init__(self, path: Path, registry: ModelRegistry) -> None:
        self._path = path
        self._registry = registry
        self._model: str | None = None
        self._custom: list[CustomModel] = []
        try:
            stored = json.loads(path.read_text())
            self._custom = [
                CustomModel(str(c["id"]), str(c["provider"]))
                for c in stored.get("custom", [])
                if c.get("id") and c.get("provider")
            ]
            model = stored.get("model")
            self._model = str(model) if model else None
        except OSError, ValueError, KeyError, TypeError:
            pass
        for custom in self._custom:
            self._register(custom)

    @property
    def model(self) -> str | None:
        """The person's pick, or None to let Vision use its default and fall back."""
        return self._model

    @property
    def custom(self) -> list[CustomModel]:
        return list(self._custom)

    def set(self, model: str | None) -> str | None:
        if model is not None and model not in self._registry:
            raise KeyError(f"{model!r} is not on the roster.")
        self._model = model
        self._save()
        return self._model

    def add_custom(self, model_id: str, provider: str) -> ModelInfo:
        """Put a verified slug on the roster and remember it across restarts."""
        custom = CustomModel(model_id, provider)
        if custom not in self._custom:
            self._custom.append(custom)
        info = self._register(custom)
        self._save()
        return info

    def _register(self, custom: CustomModel) -> ModelInfo:
        existing = self._registry.get(custom.id)
        # A transcribed row knows its real limits; keep it. A row the key check discovered
        # carries a twenty-a-day guess, which would stop a goal run, so the person's explicit
        # pick replaces it with the budget below.
        if existing is not None and not existing.discovered:
            return existing
        from ..llm.providers import get as provider_spec

        paid = provider_spec(custom.provider).paid
        info = ModelInfo(
            id=custom.id,
            label=custom.id.split("/")[-1],
            kind="text",
            summary=(
                "Added by you. A paid account, so only your own spend limit applies."
                if paid
                else "Added by you. The provider publishes no limit for it, so a modest "
                "budget of 20 a minute and 500 a day is assumed."
            ),
            discovered=True,
            provider=custom.provider,
            rpm=NO_LIMIT if paid else CUSTOM_LIMITS["rpm"],
            tpm=NO_LIMIT if paid else CUSTOM_LIMITS["tpm"],
            rpd=0 if paid else CUSTOM_LIMITS["rpd"],
        )
        self._registry.add(info, replace=True)
        log.info("vision.model.custom_added", model=custom.id, provider=custom.provider)
        return info

    def _save(self) -> None:
        data: dict[str, Any] = {
            "model": self._model,
            "custom": [{"id": c.id, "provider": c.provider} for c in self._custom],
        }
        try:
            self._path.write_text(json.dumps(data, indent=2))
        except OSError:
            log.warning("vision.model.save_failed", path=str(self._path), exc_info=True)
