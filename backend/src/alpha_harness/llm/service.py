"""Calling the model.

Every call goes through :meth:`LLMService.generate`, which does the same four things in
the same order: pick a key with budget, send, record what it actually cost, and — when
Google disagrees with our accounting — mark that pair spent and try the next key rather
than retrying into the same wall.

Rotation chooses by remaining daily budget and believes a ``429`` immediately, because a
retry loop that tries each key in turn burns a request from every key on a bad day.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from .budget import Ledger
from .context import ContextBuilder, estimate_tokens
from .keys import BudgetExhaustedError, KeyStore, LLMError
from .registry import DEFAULT_MODEL, ModelInfo, ModelRegistry

if TYPE_CHECKING:
    from ..catalog.queries import CatalogQueries
    from ..db.sqlite import Database
    from ..sealing import Sealer

log = structlog.get_logger(__name__)

#: How long a single generation may take before we give up on it.
TIMEOUT_SECONDS = 180.0


@dataclass(slots=True)
class Answer:
    """One model response, with everything it cost."""

    text: str
    model: str
    key_id: int
    key_hint: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0
    #: What was put in front of the model, so the user can see it. "Hide nothing."
    context: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "keyId": self.key_id,
            "keyHint": self.key_hint,
            "usage": {
                "promptTokens": self.prompt_tokens,
                "outputTokens": self.output_tokens,
                "thinkingTokens": self.thinking_tokens,
                "totalTokens": self.total_tokens,
            },
            "context": self.context,
            "attempts": self.attempts,
        }


def _is_rate_limit(exc: Exception) -> tuple[bool, bool]:
    """``(rate limited, daily)`` — read from whatever the SDK gives us.

    The SDK's exception types have moved between releases, so this reads the message and
    any ``code``/``status`` attribute rather than catching a class that may be renamed.
    """
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc).lower()
    limited = code == 429 or "429" in text or "resource_exhausted" in text or "quota" in text
    daily = "per day" in text or "perday" in text or "daily" in text or "requests per day" in text
    return limited, daily


def _plain_reason(exc: Exception) -> str | None:
    """The common failures, said in words the person reading them can act on.

    Google's errors arrive as a wall of JSON; the raw text is still kept against the key as
    ``lastError`` for anyone who wants it.
    """
    text = str(exc).lower()
    if "api_key_invalid" in text or "api key not valid" in text:
        return (
            "That Google AI Studio key was not accepted. Check it was copied whole, and "
            "that it has not been deleted at aistudio.google.com."
        )
    if "permission_denied" in text or "403" in text:
        return (
            "Google refused that key. It may not have access to this model, or the "
            "project it belongs to may have been closed."
        )
    if "not found" in text and "model" in text:
        return "That model is no longer available from Google. Pick a different one."
    return None


#: Gemini 2.5 takes a token budget rather than a level; Google's own mapping, with 0 for
#: minimal (thinking off).
_BUDGETS = {"MINIMAL": 0, "LOW": 1_024, "MEDIUM": 8_192, "HIGH": 24_576}

#: Models that reject ``minimal``: "not supported for Gemini 3.8 Flash" per Google's docs.
_NO_MINIMAL = ("gemini-3.7", "gemini-3.8")


def _thinking_config(model: ModelInfo, thinking: str | None) -> Any:
    """Callers ask for a level; this speaks whichever form the chosen model accepts.

    Each generation rejects the other's form, so the wrong one is a 400 on every message:
    Gemini 3 takes ``thinking_level`` (with no ``minimal`` from 3.7), 2.5 takes
    ``thinking_budget``, and Gemma, embeddings and older models take neither.
    """
    if not thinking or model.provider != "google" or model.kind != "text":
        return None
    from google.genai import types

    level = thinking.upper()
    if model.id.startswith("gemini-2.5"):
        return types.ThinkingConfig(thinking_budget=_BUDGETS[level])
    if not model.id.startswith("gemini-3"):
        return None
    if level == "MINIMAL" and model.id.startswith(_NO_MINIMAL):
        level = "LOW"
    return types.ThinkingConfig(thinking_level=types.ThinkingLevel(level))


class LLMService:
    """The assistant: keys, budget, context and prompts in one place."""

    def __init__(
        self,
        db: Database,
        sealer: Sealer,
        queries: CatalogQueries,
        registry: ModelRegistry,
    ) -> None:
        self.db = db
        self.registry = registry
        self.ledger = Ledger(db)
        self.keys = KeyStore(db, sealer, self.ledger)
        self.context = ContextBuilder(queries)
        self._clients: dict[int, Any] = {}

    # -- plumbing --------------------------------------------------------

    def _client(self, key_id: int, secret: str, provider: str = "google") -> Any:
        """The client for one key. Google speaks its own protocol; everything else
        speaks chat-completions, which one small client covers."""
        client = self._clients.get(key_id)
        if client is None:
            if provider == "google":
                from google import genai

                client = genai.Client(api_key=secret)
            else:
                from .openai_compat import OpenAICompatible
                from .providers import get as provider_spec

                spec = provider_spec(provider)
                if not spec.base_url:
                    raise LLMError(f"{provider!r} is not a provider this application knows.")
                client = OpenAICompatible(spec.base_url, secret, timeout=TIMEOUT_SECONDS)
            self._clients[key_id] = client
        return client

    def forget(self, key_id: int) -> None:
        self._clients.pop(key_id, None)

    def model_for(self, model_id: str | None) -> ModelInfo:
        return self.registry.require(model_id or DEFAULT_MODEL)

    # -- the one call ----------------------------------------------------

    async def generate(
        self,
        *,
        system: str,
        user: str,
        model_id: str | None = None,
        response_schema: Any = None,
        temperature: float = 0.7,
        thinking: str | None = None,
    ) -> Answer:
        """Send one prompt, rotating keys by remaining budget.

        ``thinking`` is one of Google's thinking levels. Not a free upgrade: thinking tokens
        are billed against the same per-minute budget as the answer.
        """
        from google.genai import types

        model = self.model_for(model_id)
        estimate = estimate_tokens(system) + estimate_tokens(user)
        if estimate > model.tpm:
            # No wait fits a request bigger than the whole per-minute budget: say so, rather
            # than a budget error that asks to try again in 0 seconds, forever.
            raise LLMError(
                f"This request is about {estimate:,} tokens, more than {model.label} accepts "
                f"in a minute ({model.tpm:,}). Use a model with a larger limit or ask less."
            )
        attempts: list[dict[str, Any]] = []
        tried: set[int] = set()

        thinking_config = _thinking_config(model, thinking)
        # Only set what is asked for: unset fields stay out of the request payload.
        options: dict[str, Any] = {}
        if thinking_config:
            options["thinking_config"] = thinking_config
        if response_schema:
            options["response_mime_type"] = "application/json"
            options["response_schema"] = response_schema
        config = types.GenerateContentConfig(
            system_instruction=system, temperature=temperature, **options
        )

        while True:
            try:
                key_id = await self.keys.choose(model, estimated_tokens=estimate)
            except BudgetExhaustedError as exc:
                if attempts:
                    # Some key worked earlier in this loop's life; report both facts.
                    exc.args = (f"{exc.args[0]} Already tried: {len(attempts)} key(s).",)
                raise

            if key_id in tried:
                # choose() keeps returning a key we already failed on, which means our
                # accounting and Google's disagree in a way one more call will not fix.
                raise LLMError(
                    "Every key with budget left has just been rejected by Google. Its "
                    "limits may have changed. Try again in a minute, or add another key."
                )
            tried.add(key_id)

            row = await self.keys.get(key_id)
            secret = await self.keys.secret(key_id)
            provider = str(getattr(row, "provider", "google") or "google")
            client = self._client(key_id, secret, provider)

            try:
                call = (
                    client.aio.models.generate_content(model=model.id, contents=user, config=config)
                    if provider == "google"
                    else client.generate(
                        model=model.id,
                        system=system,
                        user=user,
                        temperature=temperature,
                        json_mode=response_schema is not None,
                    )
                )
                response = await asyncio.wait_for(call, timeout=TIMEOUT_SECONDS)
            except TimeoutError as exc:
                await self.keys.mark(key_id, error="Timed out")
                raise LLMError(
                    f"{model.label} did not answer within {TIMEOUT_SECONDS:.0f} seconds. "
                    "Try a smaller question or a lighter model."
                ) from exc
            except Exception as exc:
                limited, daily = _is_rate_limit(exc)
                attempts.append({"keyId": key_id, "error": str(exc)[:300], "rateLimited": limited})
                await self.keys.mark(key_id, error=str(exc)[:300])
                if limited:
                    # Google is the authority: mark this pair spent and rotate rather than
                    # retrying into a limit we evidently mis-tracked.
                    await self.ledger.penalise(
                        key_id, model, daily=daily, cap=row.daily_limit if row else None
                    )
                    self.forget(key_id)
                    log.warning("llm.rate_limited", key_id=key_id, model=model.id, daily=daily)
                    continue
                plain = _plain_reason(exc)
                raise LLMError(plain or f"{model.label} could not be reached: {exc}") from exc

            usage = getattr(response, "usage_metadata", None)
            total = int(getattr(usage, "total_token_count", 0) or 0) or estimate
            await self.ledger.record(key_id, model.id, total)
            await self.keys.mark(key_id)

            return Answer(
                text=(response.text or "").strip(),
                model=model.id,
                key_id=key_id,
                key_hint=row.hint if row else "",
                prompt_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
                output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
                thinking_tokens=int(getattr(usage, "thoughts_token_count", 0) or 0),
                total_tokens=total,
                attempts=attempts,
            )

    # -- health ----------------------------------------------------------

    async def check_key(self, key_id: int) -> dict[str, Any]:
        """Confirm a key works, without spending a generation request.

        ``models.list`` is not billed against the generation quotas, so a health check
        costs nothing that matters.
        """
        row = await self.keys.get(key_id)
        provider = str(getattr(row, "provider", "google") or "google")
        secret = await self.keys.secret(key_id)
        client = self._client(key_id, secret, provider)
        try:
            if provider == "google":
                names = [m.name or "" async for m in await client.aio.models.list()]
            else:
                names = await client.models()
        # Any provider failure is the key test's answer, stored and shown to the user.
        except Exception as exc:  # noqa: BLE001
            await self.keys.mark(key_id, error=str(exc)[:300])
            return {"keyId": key_id, "ok": False, "error": str(exc)[:300]}

        await self.keys.mark(key_id)
        added = self.registry.merge_discovered(names, provider)
        return {"keyId": key_id, "ok": True, "models": len(names), "newModels": added}

    async def check_all(self) -> list[dict[str, Any]]:
        return [await self.check_key(row.id) for row in await self.keys.list_keys()]
