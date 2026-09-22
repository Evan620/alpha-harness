"""Which assistants can answer, and how to get a key for each.

Every provider here has a **free tier that needs no card**. That is the whole selection
rule: the assistant is optional, so a provider that asks for payment details first turns an
optional convenience into a purchase decision.

All of them except Google speak the OpenAI chat-completions protocol, which is why one
small client in :mod:`.openai_compat` serves seven of them. Google keeps its own path
because ``google-genai`` gives structured output and thinking levels that the
chat-completions shape cannot express.

**A key only ever answers for its own provider**, so rotation filters on provider before it
looks at budget.

**The limits below are transcribed, not measured**, read off documentation pages and kept
deliberately conservative: a budget guessed high spends someone's day before anyone notices
it was wrong, and a budget guessed low only costs a rotation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .registry import ModelInfo


@dataclass(frozen=True, slots=True)
class Provider:
    """One assistant, and what it takes to start using it."""

    id: str
    label: str
    #: Empty for Google, which speaks its own protocol rather than chat-completions.
    base_url: str
    #: Where to get a key. Shown as a link, because "search for it" loses people.
    onboarding_url: str
    #: What a key from this provider looks like, so a pasted wrong one is caught early.
    key_hint: str
    #: What this tier actually gives you, in plain words.
    tier_note: str
    #: True for a provider that bills the user. Kept apart everywhere it is shown, and its
    #: keys are refused without a daily cap — see :meth:`KeyStore.add`.
    paid: bool = False
    models: tuple[ModelInfo, ...] = ()

    @property
    def openai_compatible(self) -> bool:
        return bool(self.base_url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "baseUrl": self.base_url,
            "onboardingUrl": self.onboarding_url,
            "keyHint": self.key_hint,
            "tierNote": self.tier_note,
            "paid": self.paid,
            "openaiCompatible": self.openai_compatible,
            "models": [m.to_dict() for m in self.models],
        }


def _model(
    model_id: str,
    label: str,
    provider: str,
    *,
    rpm: int,
    rpd: int,
    tpm: int = 60_000,
    summary: str = "",
    bulk: bool = False,
    recommended: bool = False,
) -> ModelInfo:
    return ModelInfo(
        id=model_id,
        label=label,
        kind="text",
        rpm=rpm,
        tpm=tpm,
        rpd=rpd,
        summary=summary,
        bulk=bulk,
        recommended=recommended,
        provider=provider,
    )


PROVIDERS: dict[str, Provider] = {
    "google": Provider(
        id="google",
        label="Google AI Studio",
        base_url="",
        onboarding_url="https://aistudio.google.com/apikey",
        key_hint="AIza…",
        tier_note=(
            "Free with a Google account, no card. The Lite models give five hundred "
            "requests a day; the full Flash models give twenty."
        ),
        # Google's roster is the built-in table in `registry`, which carries transcribed
        # per-model budgets. Repeating it here would give it two sources of truth.
        models=(),
    ),
    "groq": Provider(
        id="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        onboarding_url="https://console.groq.com/keys",
        key_hint="gsk_…",
        tier_note="Free, no card. The fastest answers of anything on this list.",
        models=(
            _model(
                "llama-3.3-70b-versatile",
                "Llama 3.3 70B",
                "groq",
                rpm=30,
                rpd=1_000,
                summary="Strong general model, a thousand requests a day.",
                recommended=True,
            ),
            _model(
                "llama-3.1-8b-instant",
                "Llama 3.1 8B Instant",
                "groq",
                rpm=30,
                rpd=14_400,
                summary="Small and effectively unlimited by daily count. For bulk work.",
                bulk=True,
            ),
        ),
    ),
    "cerebras": Provider(
        id="cerebras",
        label="Cerebras",
        base_url="https://api.cerebras.ai/v1",
        onboarding_url="https://cloud.cerebras.ai/",
        key_hint="csk-…",
        tier_note="Free tier, no card. Very fast, with a generous daily allowance.",
        models=(
            _model(
                "llama-3.3-70b",
                "Llama 3.3 70B",
                "cerebras",
                rpm=30,
                rpd=14_400,
                summary="A large model with a bulk-sized daily budget.",
                bulk=True,
                recommended=True,
            ),
            _model(
                "qwen-3-32b",
                "Qwen 3 32B",
                "cerebras",
                rpm=30,
                rpd=14_400,
                summary="Smaller and quicker, same generous budget.",
                bulk=True,
            ),
        ),
    ),
    "openrouter": Provider(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        onboarding_url="https://openrouter.ai/keys",
        key_hint="sk-or-…",
        tier_note=(
            "One key, many models. Anything whose name ends in ':free' costs nothing, "
            "with a shared daily cap across them."
        ),
        models=(
            _model(
                "deepseek/deepseek-r1:free",
                "DeepSeek R1 (free)",
                "openrouter",
                rpm=20,
                rpd=50,
                summary="A reasoning model at no cost. Slow, and worth it for hard questions.",
            ),
            _model(
                "meta-llama/llama-3.3-70b-instruct:free",
                "Llama 3.3 70B (free)",
                "openrouter",
                rpm=20,
                rpd=50,
                summary="A solid general model on the free pool.",
                recommended=True,
            ),
        ),
    ),
    "nvidia": Provider(
        id="nvidia",
        label="NVIDIA NIM",
        base_url="https://integrate.api.nvidia.com/v1",
        onboarding_url="https://build.nvidia.com/",
        key_hint="nvapi-…",
        tier_note="Free credits with an NVIDIA account, no card.",
        models=(
            _model(
                "meta/llama-3.3-70b-instruct",
                "Llama 3.3 70B",
                "nvidia",
                rpm=40,
                rpd=1_000,
                summary="Hosted on NVIDIA's own inference stack.",
                recommended=True,
            ),
        ),
    ),
    "mistral": Provider(
        id="mistral",
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        onboarding_url="https://console.mistral.ai/api-keys/",
        key_hint="…",
        tier_note="Free experiment tier, no card. Rate limited rather than capped.",
        models=(
            _model(
                "mistral-small-latest",
                "Mistral Small",
                "mistral",
                rpm=60,
                rpd=1_000,
                summary="Quick and capable enough for picking fields.",
                recommended=True,
            ),
            _model(
                "open-mistral-nemo",
                "Mistral Nemo",
                "mistral",
                rpm=60,
                rpd=1_000,
                summary="Open-weight, smaller, cheaper on tokens.",
                bulk=True,
            ),
        ),
    ),
    "github": Provider(
        id="github",
        label="GitHub Models",
        base_url="https://models.github.ai/inference",
        onboarding_url="https://github.com/settings/tokens",
        key_hint="ghp_… or github_pat_…",
        tier_note=(
            "Free with any GitHub account — use a personal access token with the "
            "models scope. Low daily limits, but nothing new to sign up for."
        ),
        models=(
            _model(
                "openai/gpt-4o-mini",
                "GPT-4o mini",
                "github",
                rpm=15,
                rpd=150,
                summary="A capable small model on GitHub's free allowance.",
                recommended=True,
            ),
        ),
    ),
    "huggingface": Provider(
        id="huggingface",
        label="Hugging Face",
        base_url="https://router.huggingface.co/v1",
        onboarding_url="https://huggingface.co/settings/tokens",
        key_hint="hf_…",
        tier_note="Free monthly credits with a Hugging Face account, no card.",
        models=(
            _model(
                "meta-llama/Llama-3.3-70B-Instruct",
                "Llama 3.3 70B",
                "huggingface",
                rpm=10,
                rpd=200,
                summary="Routed to whichever provider is serving it.",
                recommended=True,
            ),
        ),
    ),
    # --- bring your own, and your own bill ---------------------------------------------
    #
    # Everything above is free because the assistant is optional and a card turns an
    # optional convenience into a purchase decision. These two are for the user who already
    # pays for one of them and would rather spend that than a free tier that runs out by
    # lunchtime. They are kept behind their own heading in the UI so the default stays
    # "no card", and a key for either is refused without a daily request cap: the free
    # providers stop on their own, and these stop only when told to.
    "openai": Provider(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        onboarding_url="https://platform.openai.com/api-keys",
        key_hint="sk-…",
        tier_note=(
            "Your own OpenAI account — billed to you, not free. The models appear once the "
            "key has been checked, because which ones an account can reach depends on it."
        ),
        paid=True,
        # Discovered from the key's own ``GET /models`` rather than transcribed: OpenAI's
        # roster moves, and a stale id here would fail with "model not found" on a key that
        # is perfectly good.
        models=(),
    ),
    "anthropic": Provider(
        id="anthropic",
        # Anthropic's OpenAI-compatible endpoint, which takes the same Bearer token and the
        # same ``/chat/completions`` shape, so no second client is needed. Anthropic calls
        # it a compatibility layer for evaluation rather than a production path; the ways it
        # differs that matter here are that ``response_format`` is ignored — callers already
        # ask for JSON in the prompt and parse defensively — and that thinking output is not
        # returned, which nothing here reads.
        label="Anthropic Claude",
        base_url="https://api.anthropic.com/v1",
        onboarding_url="https://platform.claude.com/settings/keys",
        key_hint="sk-ant-…",
        tier_note="Your own Anthropic account — billed to you, not free.",
        paid=True,
        models=(
            _model(
                "claude-sonnet-5",
                "Claude Sonnet 5",
                "anthropic",
                rpm=50,
                rpd=0,
                summary="The balanced choice. Fast enough for chat, strong on hard questions.",
                recommended=True,
            ),
            _model(
                "claude-opus-5",
                "Claude Opus 5",
                "anthropic",
                rpm=50,
                rpd=0,
                summary="The most capable, and the most expensive. For questions worth it.",
            ),
            _model(
                "claude-haiku-4-5-20251001",
                "Claude Haiku 4.5",
                "anthropic",
                rpm=50,
                rpd=0,
                summary="Small and cheap. The one to point bulk work at.",
                bulk=True,
            ),
        ),
    ),
    # Local addition: the operator already holds a GLM Coding Plan subscription. The base URL
    # must be the CODING path; a Coding Plan key on the general endpoint is rejected with
    # "1113 Insufficient balance". Both models reason before answering (~75 tokens), so never
    # send a small max_tokens or the content comes back empty with no error.
    "zai": Provider(
        id="zai",
        label="Z.AI (GLM)",
        base_url="https://api.z.ai/api/coding/paas/v4",
        onboarding_url="https://z.ai/manage-apikey/apikey-list",
        key_hint="32 characters, a dot, then 16",
        tier_note="Your GLM Coding Plan subscription. Limits below are the plan's.",
        paid=True,
        models=(
            _model(
                "glm-5.3",
                "GLM-5.3",
                "zai",
                rpm=20,
                rpd=2_000,
                summary="Strong reasoning model, 1M context. The better answer of the two.",
                recommended=True,
            ),
            _model(
                "glm-5.3-flash",
                "GLM-5.3 Flash",
                "zai",
                rpm=30,
                rpd=5_000,
                summary="Faster and cheaper on the same plan. For bulk work.",
                bulk=True,
            ),
        ),
    ),
}

DEFAULT_PROVIDER = "google"

#: ``rpd`` on a paid model. There is no daily ceiling but the user's own, so the key's cap
#: is what a request is measured against — see :meth:`Ledger.headroom`.
UNCAPPED = 0

#: Said once, where the numbers are shown, rather than repeated per provider.
LIMITS_NOTE = (
    "The providers above are free and need no card. The daily limits are transcribed "
    "from each provider's own documentation and will drift — they are kept deliberately "
    "low, because a budget guessed high spends your day before you notice it was wrong."
)

#: Said once, under the paid heading. The cap is the whole point: a free tier stops by
#: itself and a paid account does not, so the app will not hold one of these keys until it
#: has been told where to stop.
PAID_NOTE = (
    "These bill your own account. Nothing here is needed — every provider above is free — "
    "but a key you already pay for does not run out at lunchtime. You set a daily request "
    "cap when you add one, and Alpha Harness stops at it."
)


def provider_models() -> list[ModelInfo]:
    """Every model reachable through a provider other than Google."""
    return [model for provider in PROVIDERS.values() for model in provider.models]


def get(provider_id: str | None) -> Provider:
    """One provider, or Google when nothing was said."""
    return PROVIDERS.get(provider_id or DEFAULT_PROVIDER, PROVIDERS[DEFAULT_PROVIDER])


def catalogue() -> dict[str, Any]:
    """Every provider, Google first and the paid ones last, for the screen where a key is
    added. The order is the argument: free is the default because it is first and because
    nothing below the fold is needed to use the app."""
    free = [PROVIDERS[DEFAULT_PROVIDER]] + [
        p for p in PROVIDERS.values() if p.id != DEFAULT_PROVIDER and not p.paid
    ]
    paid = [p for p in PROVIDERS.values() if p.paid]
    return {
        "providers": [p.to_dict() for p in (*free, *paid)],
        "default": DEFAULT_PROVIDER,
        "note": LIMITS_NOTE,
        "paidNote": PAID_NOTE,
    }
