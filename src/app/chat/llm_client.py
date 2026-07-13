"""Provider-neutral LLM client interface (ADR-033).

Defines the provider-agnostic contract that the orchestrator and BYOK depend on, so the same
code base serves both Anthropic and OpenAI instances (one provider per instance, selected by
``LLM_PROVIDER``, default ``anthropic`` → existing instances are unchanged).

The neutral types (``LLMResult`` / ``LLMUsage``) generalize the former ``AnthropicResult`` /
``AnthropicUsage`` with the SAME fields — ``anthropic_client`` re-exports them as aliases for
backward compatibility. ``KeyValidation`` is reused as-is (ADR-016).

Canonical ``stop_reason`` ∈ {``tool_use``, ``max_tokens``, ``end_turn``}: the ONLY values the
orchestrator dispatches on (ADR-025). Each client maps its wire stop_reason to these constants so
the orchestrator never compares against provider-specific literals.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from app.chat.attachments import PreparedAttachments
from app.config import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a circular import at runtime
    from app.chat.openai_client import OpenAIClient

# Canonical (provider-neutral) stop_reason values — the only ones the orchestrator dispatches on
# (ADR-033 §2 / ADR-025). Each client maps its wire stop_reason to one of these.
STOP_REASON_TOOL_USE = "tool_use"
STOP_REASON_MAX_TOKENS = "max_tokens"
STOP_REASON_END_TURN = "end_turn"


class KeyValidation(str, enum.Enum):
    """Outcome of a BYOK key validation call (ADR-016, provider-neutral — ADR-033 §7).

    valid   — the provider accepted the key.
    invalid — the provider rejected the key with 401 Unauthorized.
    offline — validation could not be performed (timeout/connection/non-401 status), NOT a 401.
    """

    valid = "valid"
    invalid = "invalid"
    offline = "offline"


@dataclass(frozen=True)
class LLMUsage:
    """Provider-neutral token usage (ADR-033 §1, generalizes AnthropicUsage).

    For OpenAI, ``cache_read_tokens`` is ``prompt_tokens_details.cached_tokens`` (0 when absent)
    and ``cache_write_tokens`` is always 0 (OpenAI auto-caches; there is no explicit write count).
    """

    input_tokens: int
    output_tokens: int
    model: str
    cache_read_tokens: int
    cache_write_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "model": self.model,
            "cacheReadTokens": self.cache_read_tokens,
            "cacheWriteTokens": self.cache_write_tokens,
        }


@dataclass(frozen=True)
class LLMResult:
    """Provider-neutral generation result (ADR-033 §1, generalizes AnthropicResult).

    - ``stop_reason``: canonical value ({tool_use, max_tokens, end_turn}); the orchestrator never
      sees provider wire stop reasons.
    - ``content_blocks``: WIRE format of the ACTIVE provider, ALREADY normalized at the persist
      boundary (per-provider allowlist, ADR-021). Stored verbatim in ``chat_steps.payload`` and
      replayed by the same client (one provider per instance, ADR-033 §3).
    - ``usage``: neutral token usage.
    - ``text``: concatenated assistant text of this turn.
    - ``tool_uses``: DOMAIN-shaped ``{id(provider raw), name(domain dotted), input(dict)}``. The
      client has already reverse-mapped the name and (for OpenAI) parsed ``arguments`` JSON → dict,
      so the orchestrator gets a homogeneous result regardless of provider.
    """

    stop_reason: str | None
    content_blocks: list[dict[str, Any]]
    usage: LLMUsage
    text: str = ""
    tool_uses: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class GenerationOptions:
    """Per-call generation options (ADR-059 / ADR-055), passed as a single value-object.

    Extends ``LLMClient.create_message`` with one optional parameter so the Protocol stays
    backward compatible — a client that ignores it (Anthropic — smart-only, ADR-059 §5) still
    satisfies the contract, and existing callers/tests that omit it are unchanged.

    - ``dialog_mode``: the session dialog mode, constrained to the fixed ADR-055 domain
      (``None`` == smart). Typed as a ``Literal`` (not a free ``str``) so a stray value is a static
      type error, while runtime stays permissive — request-value validation lives in the
      orchestrator (an unknown mode from the API → ``422 unsupported_dialog_mode``, NOT a client
      ValueError). The OpenAI Responses client branches on it (ADR-055 §5): deep_thinking →
      reasoning + encrypted-content replay; search → the built-in web_search tool; study_learn →
      prompt suffix + the live quiz.generate strict tool (ADR-062). Anthropic ignores it
      (smart-only, ADR-059 §5).
    - ``temporary``: when True the OpenAI Responses client sets ``store=False`` so the provider does
      not persist the request (ADR-056). Ignored by Anthropic.
    """

    dialog_mode: Literal["smart", "deep_thinking", "study_learn", "search"] | None = None
    temporary: bool = False


@dataclass(frozen=True)
class NeutralMessage:
    """One step of the provider-neutral history passed to ``LLMClient.create_message`` (ADR-033 §3).

    - role ``user``/``assistant``: ``content_blocks`` are the wire blocks of the active provider
      from ``chat_steps.payload`` (the client replays them verbatim).
    - role ``tool``: ``content_blocks`` is unused; the domain tool-result fields carry the data the
      client needs to build the provider tool message (Anthropic ``tool_result`` block / OpenAI
      ``role=tool`` message). ``provider_tool_use_id`` is the raw provider id (toolu_.../call_...),
      used to correlate tool_use ↔ tool_result on replay (ADR-008, generalized by ADR-033 §3).
    """

    role: str
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    # tool-step fields (role == "tool"): the domain tool-result record.
    tool_call_id: str | None = None
    provider_tool_use_id: str | None = None
    tool_name: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@runtime_checkable
class LLMClient(Protocol):
    """Provider-agnostic LLM client contract (ADR-033 §1).

    The orchestrator and BYOK depend on this Protocol, not on a concrete client. All
    provider-specific (de)serialization of the wire format lives INSIDE the implementations
    (AnthropicClient / OpenAIClient) — the orchestrator passes neutral data and gets an LLMResult.
    """

    async def create_message(
        self,
        *,
        system_prompt: str,
        messages: list[NeutralMessage],
        tools: list[dict[str, Any]],
        attachments: PreparedAttachments | None = None,
        api_key: str | None = None,
        # ADR-034 §4: optional per-call model override. None → the client uses its provider default
        # (settings.<provider>_model) — the current behavior, unchanged for existing callers/tests.
        model: str | None = None,
        # ADR-059 / ADR-055: optional generation options (dialog_mode / temporary). One optional
        # parameter with a default — a client that ignores it (Anthropic) still satisfies the
        # Protocol and existing callers/tests that omit it are unchanged.
        options: GenerationOptions | None = None,
    ) -> LLMResult: ...

    async def validate_key(self, api_key: str) -> KeyValidation: ...


_openai_singleton: OpenAIClient | None = None


def _get_openai_singleton() -> LLMClient:
    """Process-wide OpenAI client singleton (shared by ``get_llm_client`` and ``llm_client_for``).

    Lazily constructed once per process. The OpenAI client constructor reads only config
    (``OPENAI_API_KEY``/``OPENAI_MODEL``) and does NOT depend on ``LLM_PROVIDER`` — so it can be
    created on any instance (e.g. an OpenAI-BYOK call on an Anthropic instance, ADR-044 §2).
    """
    global _openai_singleton
    if _openai_singleton is None:
        from app.chat.openai_client import OpenAIClient

        _openai_singleton = OpenAIClient()
    return _openai_singleton


def llm_client_for(provider: str) -> LLMClient:
    """Return the LLMClient for an EXPLICIT provider, independent of ``LLM_PROVIDER`` (ADR-044 §2).

    Used by the multi-provider BYOK path (validation + generation) so a key is always handled by the
    provider DETECTED from the key itself (``detect_byok_provider``), not by the instance's active
    provider. Both clients are process-wide singletons available on any instance:

    - ``"anthropic"`` → the shared ``anthropic_client`` module singleton (same as
      ``get_anthropic_client()``), so a conftest patch of ``_anthropic_singleton`` overrides this
      path too.
    - ``"openai"`` → the shared OpenAI singleton (same instance ``get_llm_client()`` uses on the
      openai path).
    - any other value → ``ValueError`` (internal caller error; ``detect_byok_provider`` guarantees
      only ``{"anthropic", "openai"}`` before this is called).

    The imports are local to avoid an import cycle (clients import the neutral types here).
    """
    normalized = provider.strip().lower()
    if normalized == "openai":
        return _get_openai_singleton()
    if normalized == "anthropic":
        from app.chat.anthropic_client import get_anthropic_client

        return get_anthropic_client()
    raise ValueError(f"unknown LLM provider: {provider!r}")


def get_llm_client() -> LLMClient:
    """Process-wide LLMClient selected by ``LLM_PROVIDER`` (default ``anthropic``, ADR-033 §8).

    Replaces the former ``get_anthropic_client()`` singleton. ``LLM_PROVIDER=anthropic`` (the
    default) returns the unchanged ``AnthropicClient`` so existing instances keep their exact
    behavior; ``LLM_PROVIDER=openai`` returns ``OpenAIClient``. Each client is created once per
    process.

    Refactored to delegate to ``llm_client_for(active_provider)`` (single source of singletons,
    ADR-044 §2) — the public signature and behavior are UNCHANGED (still reads ``LLM_PROVIDER``). On
    the anthropic path the shared ``anthropic_client`` module singleton is used, so a test that
    patches ``anthropic_client._anthropic_singleton`` overrides the factory too (conftest).
    """
    provider = get_settings().llm_provider.strip().lower()
    if provider == "openai":
        return llm_client_for("openai")
    # Default (and explicit "anthropic"): reuse the anthropic_client module singleton.
    return llm_client_for("anthropic")
