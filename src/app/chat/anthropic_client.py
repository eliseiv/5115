"""Anthropic Claude client — an LLMClient implementation (CO-2, ADR-033).

Real integration with the Anthropic Python SDK. Supports prompt caching (cache_control
ephemeral on system prompt and tool definitions), tool-loop generation, usage parsing
(including cache read/write tokens), and a per-call api_key override for BYOK (the user's
key is passed in-memory only and never logged — 05-security.md, ADR-003).

ADR-033: this is now one implementation of the provider-neutral ``LLMClient`` Protocol. The
neutral result/usage types live in ``llm_client`` (``LLMResult``/``LLMUsage``); ``AnthropicResult``
/ ``AnthropicUsage`` / ``KeyValidation`` are kept here as backward-compatible aliases so existing
imports and tests keep working. All Anthropic-specific (de)serialization of the wire format —
building provider ``messages`` from the neutral history, the per-provider tool serialization, the
attachment content blocks and the persist-boundary normalization — lives INSIDE this client.

Model and key come from config / per-call override; never hardcoded (02-tech-stack.md).
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import anthropic

from app.chat.attachments import PreparedAttachments
from app.chat.llm_client import (
    STOP_REASON_END_TURN,
    STOP_REASON_MAX_TOKENS,
    STOP_REASON_TOOL_USE,
    GenerationOptions,
    KeyValidation,
    LLMResult,
    LLMUsage,
    NeutralMessage,
)
from app.chat.tools import (
    UnknownToolNameError,
    to_anthropic_tool_name,
    to_domain_tool_name,
)
from app.config import get_settings
from app.errors import UpstreamError, ValidationFailedError
from app.observability.logging import get_logger, log_event
from app.observability.metrics import anthropic_upstream_errors_total, llm_upstream_errors_total

_logger = get_logger("app.chat.anthropic")

_PROVIDER = "anthropic"

# Canonical stop_reason map (ADR-033 §2): Anthropic wire stop_reason → neutral value. Anything not
# tool_use/max_tokens (end_turn, stop_sequence, None, …) collapses to end_turn (a final turn).
_STOP_REASON_MAP: dict[str, str] = {
    "tool_use": STOP_REASON_TOOL_USE,
    "max_tokens": STOP_REASON_MAX_TOKENS,
}


def _to_neutral_stop_reason(wire_stop_reason: str | None) -> str:
    if wire_stop_reason is None:
        return STOP_REASON_END_TURN
    return _STOP_REASON_MAP.get(wire_stop_reason, STOP_REASON_END_TURN)


# Backward-compatible aliases (ADR-033): the neutral types replace the former Anthropic-specific
# ones with identical fields. Existing imports/tests of AnthropicResult/AnthropicUsage keep working.
AnthropicResult = LLMResult
AnthropicUsage = LLMUsage

# ADR-021: wire-valid field allowlist per content-block type for the Anthropic Messages API.
# block.model_dump() carries non-wire SDK fields (e.g. "caller": {"type": "direct"}) that are
# garbage on replay and violate the payload-purity invariant. Normalization keeps ONLY these
# fields per type (allowlist, not point-removal of `caller`) so it is robust to future SDK
# annotations. Applied ONCE at the persist boundary (when assembling content blocks from the
# Anthropic response); all later replays read already-clean blocks (hot-path continuation does
# not re-normalize). Raw tool_use.id is preserved verbatim — ADR-008 invariant.
_BLOCK_WIRE_FIELDS: dict[str, tuple[str, ...]] = {
    "text": ("type", "text"),
    "image": ("type", "source"),
    "document": ("type", "source"),
    "tool_use": ("type", "id", "name", "input"),
    "thinking": ("type", "thinking", "signature"),
    "redacted_thinking": ("type", "data"),
}


def _normalize_block(block: dict[str, Any]) -> dict[str, Any]:
    """Strip non-wire SDK fields from one content block by its type's wire allowlist (ADR-021).

    For a known block type, keep only the wire-valid fields present in the block. For an unknown
    type, drop only confirmed non-wire SDK annotations (``caller``) and keep the rest so no content
    is lost (forward-compatible with new block types).
    """
    block_type = block.get("type")
    if isinstance(block_type, str) and block_type in _BLOCK_WIRE_FIELDS:
        allowed = _BLOCK_WIRE_FIELDS[block_type]
        return {k: block[k] for k in allowed if k in block}
    # Unknown type: don't lose content — drop only known non-wire SDK fields.
    return {k: v for k, v in block.items() if k != "caller"}


class AnthropicAuthError(Exception):
    """Raised when Anthropic rejects the (BYOK) key as unauthorized → key_status=invalid."""


def _extract_error_body(exc: Exception) -> tuple[str | None, str | None]:
    """Extract Anthropic error.type / error.message from the SDK exception body (TD-014).

    The Anthropic wire error body is `{"type": "error", "error": {"type": ..., "message": ...}}`.
    `APIError.body` is the decoded JSON (or raw/None when undecodable). This is the *provider's*
    error body — never user-content — so it is safe to log (03-architecture.md §Логирование
    upstream-ошибок Anthropic). Returns (error_type, error_message); either may be None when the
    body is absent or not in the expected shape (then the field is omitted from the log).
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None, None
    error = body.get("error")
    if not isinstance(error, dict):
        return None, None
    error_type = error.get("type")
    error_message = error.get("message")
    return (
        error_type if isinstance(error_type, str) else None,
        error_message if isinstance(error_message, str) else None,
    )


def _log_upstream_error(exc: Exception, *, model: str) -> None:
    """Log the `anthropic_upstream_error` event BEFORE mapping to UpstreamError (TD-014).

    Logs only the provider error body (status_code / error.type / error.message), the anthropic
    request id, the model and the exception class — never the api-key, BYOK key or user-content
    (03-architecture.md §Логирование upstream-ошибок Anthropic, 05-security.md §Логирование).
    Correlation fields (requestId/sessionId) are attached automatically by the log formatter.
    Levels per the TD-014 matrix: WARNING for 4xx (incl. 429), ERROR for 5xx and for
    timeout/connection errors (which carry no HTTP status). Two metrics are incremented with
    bounded-enum labels: the legacy ``anthropic_upstream_errors_total`` (kept for existing
    dashboards/tests) and the generalized ``llm_upstream_errors_total{provider}`` (ADR-033 §10).
    """
    status_code: int | None = getattr(exc, "status_code", None)
    error_type, error_message = _extract_error_body(exc)
    anthropic_request_id: str | None = getattr(exc, "request_id", None)

    if status_code is not None and 400 <= status_code < 500:
        level = logging.WARNING
    else:
        # 5xx, or timeout/connection errors with no HTTP status.
        level = logging.ERROR

    fields: dict[str, Any] = {
        "event": "anthropic_upstream_error",
        "model": model,
        "exceptionClass": type(exc).__name__,
    }
    if status_code is not None:
        fields["status_code"] = status_code
    if error_type is not None:
        fields["errorType"] = error_type
    if error_message is not None:
        fields["errorMessage"] = error_message
    if anthropic_request_id is not None:
        fields["anthropicRequestId"] = anthropic_request_id

    log_event(_logger, level, "anthropic_upstream_error", **fields)
    status_label = str(status_code) if status_code is not None else "none"
    type_label = error_type or "unknown"
    # Legacy metric (existing dashboards/tests) + generalized provider-labeled metric (ADR-033 §10).
    anthropic_upstream_errors_total.labels(
        status_code=status_label,
        error_type=type_label,
    ).inc()
    llm_upstream_errors_total.labels(
        provider=_PROVIDER,
        status_code=status_label,
        error_type=type_label,
    ).inc()


class AnthropicClient:
    """Thin async wrapper around anthropic.AsyncAnthropic, implementing LLMClient (ADR-033).

    The service key is read from config; BYOK callers pass api_key per call. TLS verification
    is enabled by default by the SDK (httpx). No secrets are logged.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._default_model = settings.anthropic_model
        self._max_tokens = settings.anthropic_max_tokens
        self._service_key = settings.anthropic_api_key
        # One client per process; per-call key overrides via with_options(api_key=...).
        # timeout / max_retries are config-driven (no upstream call hangs the request pool).
        self._client = anthropic.AsyncAnthropic(
            api_key=self._service_key or "placeholder",
            timeout=settings.anthropic_timeout_seconds,
            max_retries=settings.anthropic_max_retries,
        )

    def _build_system(self, system_prompt: str) -> list[dict[str, Any]]:
        # cache_control on the (stable) system prompt for prompt caching.
        return [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    @staticmethod
    def _build_provider_messages(
        messages: list[NeutralMessage] | list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Translate the neutral history into Anthropic wire messages (ADR-033 §3).

        Raw dicts are passed through unchanged (external e2e callers build messages directly). For
        NeutralMessage items: user/assistant replay their wire content blocks verbatim; a tool step
        becomes an Anthropic ``tool_result`` block carrying the RAW provider id (toolu_..., never a
        domain UUID — ADR-008/BUG-4) so the continuation history's id pair is consistent.
        """
        out: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, dict):
                out.append(msg)
                continue
            if msg.role in ("user", "assistant"):
                out.append({"role": msg.role, "content": msg.content_blocks})
            elif msg.role == "tool":
                if msg.error is not None:
                    content = str(msg.error.get("message", "tool error"))
                    is_error = True
                else:
                    content = json.dumps(msg.result)
                    is_error = False
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.provider_tool_use_id,
                                "content": content,
                                "is_error": is_error,
                            }
                        ],
                    }
                )
        return out

    @staticmethod
    def _serialize_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Serialize neutral tool definitions to the Anthropic wire format (ADR-033 §4).

        Neutral definition = ``{name(domain dotted), description, input_schema}``. Anthropic wants
        underscore names (BUG-3). For backward compatibility, a definition already in the Anthropic
        shape (underscore name, no dot) is passed through unchanged — ``anthropic_tool_definitions``
        callers and tests still work.
        """
        serialized: list[dict[str, Any]] = []
        for t in tools:
            name = str(t.get("name", ""))
            anthropic_name = to_anthropic_tool_name(name) if "." in name else name
            serialized.append(
                {
                    "name": anthropic_name,
                    "description": t["description"],
                    "input_schema": t["input_schema"],
                }
            )
        return serialized

    @staticmethod
    def _parse_usage(message: anthropic.types.Message, model: str) -> LLMUsage:
        usage = message.usage
        return LLMUsage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            model=model,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )

    async def create_message(
        self,
        *,
        system_prompt: str,
        messages: list[NeutralMessage] | list[dict[str, Any]],
        tools: list[dict[str, Any]],
        attachments: PreparedAttachments | None = None,
        api_key: str | None = None,
        model: str | None = None,
        options: GenerationOptions | None = None,
    ) -> LLMResult:
        """Call messages.create with prompt caching and tools (LLMClient.create_message).

        Builds the Anthropic wire messages from the neutral history, injects the attachment content
        blocks (ADR-020) into the LAST user turn on the first call only, serializes tools to the
        Anthropic format, and parses stop_reason (→ canonical), content blocks (normalized at the
        persist boundary), usage, text and tool_uses (domain names). api_key: optional per-call
        override (BYOK); None → service key. model (ADR-034 §4): optional model id; None → the
        configured default (``settings.anthropic_model``) — current behavior, unchanged.

        options (ADR-059 §5): accepted for Protocol conformance and IGNORED (graceful no-op).
        Anthropic stays smart-only — the new dialog modes (deep_thinking / study_learn / search)
        and the temporary-chat ``store=False`` flag are OpenAI/Responses-only (ADR-055 §4). No
        branching here; the parameter never changes the Anthropic request.
        """
        del options  # ADR-059 §5: explicit no-op — Anthropic ignores generation options.
        model = model if model is not None else self._default_model
        client = self._client
        if api_key is not None:
            client = client.with_options(api_key=api_key)

        wire_messages = self._build_provider_messages(messages)
        if attachments is not None and attachments.content_blocks:
            # ADR-020: inject the FULL attachment blocks into the last user turn for this single
            # call only (the persisted history holds placeholders, which the orchestrator already
            # put in the neutral content; here we replace that last user content with full blocks).
            for wm in reversed(wire_messages):
                if wm.get("role") == "user":
                    existing = wm.get("content")
                    base = existing if isinstance(existing, list) else []
                    wm["content"] = [*base, *attachments.content_blocks]
                    break

        anthropic_tools = self._serialize_tools(tools)
        # cache_control on the last tool definition caches the whole tool list + system.
        cached_tools = [dict(t) for t in anthropic_tools]
        if cached_tools:
            cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}

        try:
            message = await client.messages.create(
                model=model,
                max_tokens=self._max_tokens,
                system=cast(Any, self._build_system(system_prompt)),
                tools=cast(Any, cached_tools),
                messages=cast(Any, wire_messages),
            )
        except anthropic.AuthenticationError as exc:
            # BYOK/service key rejected → mapped to byok_invalid (block) or 401 upstream.
            # TD-014: log the upstream error (401 → WARNING) BEFORE mapping; key is never logged.
            _log_upstream_error(exc, model=model)
            raise AnthropicAuthError(str(exc)) from exc
        except (
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.APIStatusError,
        ) as exc:
            # Timeout / connection / 5xx (or other status) from Anthropic → 502 upstream_error.
            # TD-014: log the structured upstream-error event BEFORE mapping to UpstreamError;
            # only the provider error body is logged, never the api_key or user-content.
            _log_upstream_error(exc, model=model)
            raise UpstreamError("anthropic upstream error") from exc

        content_blocks: list[dict[str, Any]] = []
        text_parts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        for block in message.content:
            # ADR-021: normalize at the persist boundary — strip non-wire SDK fields (e.g.
            # `caller`) so chat_steps.payload holds only wire-valid Anthropic blocks and replays
            # carry no garbage. raw tool_use.id is kept verbatim (ADR-008).
            block_dict = _normalize_block(block.model_dump())
            content_blocks.append(block_dict)
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                # BUG-3 reverse map: Claude returns the anthropic-name (underscore). Translate to
                # the domain name (dotted) BEFORE it reaches tool_calls / toolCall.name / audit.
                # `content_blocks` keeps the raw anthropic-name on purpose: the assistant turn is
                # replayed verbatim to Anthropic on continuation and must match the wire protocol.
                try:
                    domain_name = to_domain_tool_name(block.name)
                except UnknownToolNameError as exc:
                    # Upstream anomaly: an unmapped tool name must never surface to iOS as a valid
                    # tool. Fail processing explicitly rather than forwarding garbage.
                    raise ValidationFailedError(str(exc)) from exc
                tool_uses.append({"id": block.id, "name": domain_name, "input": block.input})

        return LLMResult(
            stop_reason=_to_neutral_stop_reason(message.stop_reason),
            content_blocks=content_blocks,
            usage=self._parse_usage(message, model),
            text="".join(text_parts),
            tool_uses=tool_uses,
        )

    async def validate_key(self, api_key: str) -> KeyValidation:
        """Lightweight Anthropic call to validate a BYOK key (ADR-003 step 6, ADR-016).

        Returns:
        - KeyValidation.valid   — Anthropic accepted the key;
        - KeyValidation.invalid — 401 Unauthorized;
        - KeyValidation.offline — timeout/connection/non-401 status (cannot determine validity).
        Never logs the key.
        """
        client = self._client.with_options(api_key=api_key)
        try:
            await client.messages.create(
                model=self._default_model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
        except anthropic.AuthenticationError:
            return KeyValidation.invalid
        except (
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.APIStatusError,
        ):
            # Network/transient/non-401 status: we could not validate → offline (ADR-016).
            return KeyValidation.offline
        return KeyValidation.valid


# Process-wide Anthropic singleton. Kept as a module global so tests can patch it (conftest sets
# ``anthropic_client._anthropic_singleton = fake``); ``get_llm_client()`` honors it on the anthropic
# path so a patched fake is used uniformly (ADR-033 §8 — factory shares this singleton).
_anthropic_singleton: AnthropicClient | None = None


def get_anthropic_client() -> AnthropicClient:
    """Backward-compatible accessor (ADR-033 §8): the active LLM client, asserted Anthropic.

    Kept for existing imports/tests. Returns the shared process-wide singleton (``get_llm_client()``
    delegates here on the anthropic path), so patching ``_anthropic_singleton`` in tests overrides
    both this helper and the provider factory.
    """
    global _anthropic_singleton
    if _anthropic_singleton is None:
        _anthropic_singleton = AnthropicClient()
    return _anthropic_singleton
