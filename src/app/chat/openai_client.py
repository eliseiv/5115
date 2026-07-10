"""OpenAI client — an LLMClient implementation (ADR-033, ADR-059).

Real integration with the OpenAI Python SDK (``AsyncOpenAI``) over the **Responses API**
(``client.responses.create``), function-calling + vision, NON-streaming — parity with the current
non-streaming Anthropic path (ADR-025 §non-streaming). Active only on instances with
``LLM_PROVIDER=openai``; the default ``anthropic`` path is unchanged.

ADR-059: the client moved from Chat Completions to the Responses API (web-search / reasoning /
image generation need Responses). All OpenAI-specific (de)serialization of the Responses wire format
lives INSIDE this client (ADR-033 §3):
- builds the Responses ``input`` list from the neutral history: a user step → an
  ``{type:'message', role:'user', content:[{type:'input_text', ...}]}`` item; an assistant step →
  its persisted ``output[]`` items replayed verbatim (they are accepted back as input); a tool step
  → ``{type:'function_call_output', call_id, output}`` — plus first-turn attachment parts
  (``input_image`` / ``input_text`` / native ``input_file`` for PDF — ADR-059 §6);
- serializes tools to the FLAT Responses function-tool shape via ``tools.openai_tool_function``
  (``{type:'function', name(underscore), parameters, strict, description}`` — SSOT, ADR-059 §1);
- parses the response: presence of a ``function_call`` item → canonical ``tool_use`` stop_reason;
  else ``status=='incomplete'`` + ``incomplete_details.reason=='max_output_tokens'`` →
  ``max_tokens``; else ``end_turn`` (ADR-059 §1). ``function_call`` items → domain tool_uses
  (reverse-mapped name, ``arguments`` JSON parsed to dict; invalid JSON / unknown name →
  ValidationFailedError; the raw ``call_id`` (call_...), NOT ``id`` (fc_...), is the
  ``provider_tool_use_id`` — ADR-008/ADR-059 §1).
  ``web_search_call`` items are NEVER surfaced as tool_uses and never reverse-mapped (ADR-059 §1).
  usage: cache_read from ``input_tokens_details.cached_tokens`` or 0, cache_write always 0;
  ``content_blocks`` = the Responses ``output[]`` items for persist (so ``_build_input`` can replay
  the continuation).

``store`` defaults to the API default; a temporary chat (ADR-056) sets ``store=False``. The OpenAI
key is never logged (redaction covers ``key``/``secret``). ADR-059 §8: no backward-compatibility
read of the former Chat Completions payload — prod runs on Anthropic, there are no OpenAI sessions.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import openai
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

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
    openai_tool_function,
    to_domain_tool_name,
)
from app.config import get_settings
from app.errors import UpstreamError, ValidationFailedError
from app.observability.logging import get_logger, log_event
from app.observability.metrics import llm_upstream_errors_total

_logger = get_logger("app.chat.openai")

_PROVIDER = "openai"


def _log_upstream_error(exc: Exception, *, model: str, status_code: int | None) -> None:
    """Log ``llm_upstream_error`` BEFORE mapping (ADR-033 §10, mirrors the Anthropic path).

    Logs only non-sensitive metadata (status, exception class, model) — never the api-key or
    user-content. Level matrix mirrors TD-014: WARNING for 4xx, ERROR for 5xx / network errors.
    """
    if status_code is not None and 400 <= status_code < 500:
        level = logging.WARNING
    else:
        level = logging.ERROR
    fields: dict[str, Any] = {
        "event": "llm_upstream_error",
        "provider": _PROVIDER,
        "model": model,
        "exceptionClass": type(exc).__name__,
    }
    if status_code is not None:
        fields["status_code"] = status_code
    log_event(_logger, level, "llm_upstream_error", **fields)
    llm_upstream_errors_total.labels(
        provider=_PROVIDER,
        status_code=str(status_code) if status_code is not None else "none",
        error_type=type(exc).__name__,
    ).inc()


class OpenAIAuthError(Exception):
    """Raised when OpenAI rejects the (BYOK) key as unauthorized → key_status=invalid (ADR-016)."""


class OpenAIClient:
    """Async wrapper around ``openai.AsyncOpenAI`` (Responses API), an LLMClient (ADR-033/ADR-059).

    The service key is read from config; BYOK callers pass api_key per call. TLS verification is
    enabled by default by the SDK (httpx). No secrets are logged.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._default_model = settings.openai_model
        self._max_tokens = settings.openai_max_tokens
        self._service_key = settings.openai_api_key
        # One client per process; per-call key overrides via with_options(api_key=...).
        self._client = openai.AsyncOpenAI(
            api_key=self._service_key or "placeholder",
            timeout=settings.openai_timeout_seconds,
            max_retries=settings.openai_max_retries,
        )

    def _build_input(
        self,
        messages: list[NeutralMessage] | list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Translate the neutral history into the Responses ``input`` item list (ADR-059 §1).

        - ``user``  → a ``message`` item with ``input_text`` parts (persisted user blocks are
          provider-agnostic ``{type:'text', text}`` placeholders — ADR-020 §3).
        - ``assistant`` → its persisted ``output[]`` items (``message`` / ``reasoning`` /
          ``function_call``) replayed VERBATIM: they are accepted back as ``input`` (ADR-059 §1);
          each ``function_call`` carries the same ``call_id`` that the paired tool step references.
        - ``tool`` → a ``function_call_output`` item keyed by the raw ``call_id`` = the tool step's
          ``provider_tool_use_id`` (ADR-008/ADR-059 §1), so tool_use ↔ tool_result stay correlated.

        Raw-dict items (external e2e callers building Responses items directly) are passed through
        unchanged, mirroring the Anthropic client.
        """
        out: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, dict):
                out.append(msg)
                continue
            if msg.role == "assistant":
                # Persisted output[] items — a flat list of input-acceptable items. Replay as-is.
                out.extend(msg.content_blocks)
            elif msg.role == "user":
                out.append(self._user_item_from_blocks(msg.content_blocks))
            elif msg.role == "tool":
                payload = msg.error if msg.error is not None else msg.result
                out.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.provider_tool_use_id,
                        "output": json.dumps(payload),
                    }
                )
        return out

    @staticmethod
    def _user_item_from_blocks(blocks: list[dict[str, Any]]) -> dict[str, Any]:
        """Build a Responses user ``message`` item from persisted user blocks (ADR-059 §1).

        Persisted user content is text block(s) + light placeholders (ADR-020 §3, provider-agnostic
        ``{type:'text', text}``). Each becomes an ``input_text`` content part (the Responses user
        message requires ``input_*`` part types).
        """
        parts: list[dict[str, Any]] = []
        for b in blocks:
            if b.get("type") == "text" and isinstance(b.get("text"), str):
                parts.append({"type": "input_text", "text": b["text"]})
        return {"type": "message", "role": "user", "content": parts}

    @staticmethod
    def _inject_attachments(
        input_items: list[dict[str, Any]], attachments: PreparedAttachments
    ) -> None:
        """Append first-turn attachment parts to the LAST user message item (ADR-020 / ADR-059 §6).

        The attachment content blocks are already Responses parts (``input_image`` / ``input_text``
        / native ``input_file`` for PDF — built in ``attachments.py``). They are appended to the
        last user ``message`` item's ``content`` list. Mutates in place; no-op when none.
        """
        if not attachments.content_blocks:
            return
        for item in reversed(input_items):
            if item.get("type") == "message" and item.get("role") == "user":
                existing = item.get("content")
                parts: list[Any] = list(existing) if isinstance(existing, list) else []
                parts.extend(attachments.content_blocks)
                item["content"] = parts
                return

    @staticmethod
    def _serialize_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Serialize neutral tool defs to the flat Responses function-tool shape (ADR-059 §1).

        Delegates each neutral def to ``tools.openai_tool_function`` — the single source of truth
        for the OpenAI wire shape (in ``tools.py`` next to ``anthropic_tool_definitions``). The
        ``include_server_side`` gate was already applied by the orchestrator when building the
        neutral list (ADR-022 §A), so this only does per-item wire wrapping.
        """
        return [openai_tool_function(t) for t in tools]

    @staticmethod
    def _to_stop_reason(response: Response, *, has_function_call: bool) -> str:
        """Normalized stop_reason (ADR-033 §2 / ADR-059 §1) from a Responses ``Response``.

        A ``function_call`` item → ``tool_use``; else ``status=='incomplete'`` with
        ``incomplete_details.reason=='max_output_tokens'`` → ``max_tokens``; else ``end_turn``. A
        ``web_search_call`` item does NOT set ``tool_use`` (its caller never passes
        has_function_call for it — ADR-059 §1).
        """
        if has_function_call:
            return STOP_REASON_TOOL_USE
        if response.status == "incomplete":
            details = response.incomplete_details
            if details is not None and details.reason == "max_output_tokens":
                return STOP_REASON_MAX_TOKENS
        return STOP_REASON_END_TURN

    def _parse_usage(self, response: Response, model: str) -> LLMUsage:
        usage = response.usage
        if usage is None:
            return LLMUsage(0, 0, model, 0, 0)
        cache_read = 0
        details = usage.input_tokens_details
        if details is not None:
            cache_read = getattr(details, "cached_tokens", 0) or 0
        return LLMUsage(
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            model=model,
            cache_read_tokens=cache_read,
            cache_write_tokens=0,  # OpenAI has no explicit cache-write count (auto-cache).
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
        """Call ``responses.create`` (non-streaming) and return a neutral LLMResult (ADR-059).

        system_prompt is passed via ``instructions`` (not as a first input item). ``max_tokens`` →
        ``max_output_tokens`` (``settings.openai_max_tokens``). model (ADR-034 §4): optional model
        id; None → the configured default (``settings.openai_model``).

        options (ADR-059 / ADR-055 / ADR-056): branches the Responses request by dialog mode.
        - ``deep_thinking`` → ``reasoning={"effort": ...}`` + replay of the encrypted reasoning
          items (``store=False`` + ``include=["reasoning.encrypted_content"]`` — ADR-059 §3; without
          them OpenAI rejects the continuation), plus its own longer timeout.
        - ``search`` → the built-in ``{"type": "web_search", ...}`` tool ALONGSIDE our
          function-tools (executed by the provider within one call; ``web_search_call`` items are
          never surfaced as tool_uses — see the parser below).
        - ``study_learn`` → prompt-only this sprint (the quiz.generate tool is ADR-057/sprint 4);
          no wire change here.
        - ``temporary`` → ``store=False`` (the provider does not persist the request, ADR-056).
        Optional params use the ``openai.omit`` sentinel so they are simply not sent unless needed.
        """
        model = model if model is not None else self._default_model
        settings = get_settings()
        dialog_mode = options.dialog_mode if options is not None else None
        temporary = options is not None and options.temporary

        client = self._client
        if api_key is not None:
            client = client.with_options(api_key=api_key)
        # ADR-055 §5 / ADR-059 §3: reasoning is slower than a plain turn — give deep_thinking its
        # own per-call timeout so a long reasoning generation does not false-trip a 502.
        if dialog_mode == "deep_thinking":
            client = client.with_options(timeout=settings.deep_thinking_timeout_seconds)

        input_items = self._build_input(messages)
        if attachments is not None:
            self._inject_attachments(input_items, attachments)
        openai_tools = self._serialize_tools(tools)

        # store: API default (``openai.omit`` = "parameter not sent") unless opted out. A temporary
        # chat (ADR-056) sets store=False; deep_thinking ALSO requires store=False so the encrypted
        # reasoning items can be replayed on continuation (ADR-059 §3, set below).
        store: bool | openai.Omit = openai.omit
        if temporary:
            store = False

        # ADR-055 §5 / ADR-059 §2,§3: per-dialog-mode Responses parameters.
        reasoning: dict[str, str] | openai.Omit = openai.omit
        include: list[str] | openai.Omit = openai.omit
        if dialog_mode == "deep_thinking":
            reasoning = {"effort": settings.resolved_deep_thinking_effort()}
            # reasoning items MUST replay verbatim in the tool-loop; the encrypted payload is
            # requested via include and persisted in content_blocks (ADR-059 §3).
            include = ["reasoning.encrypted_content"]
            store = False
        elif dialog_mode == "search":
            # The GA web_search tool (ADR-059 §2) is added RIGHT NEXT TO our function-tools; the
            # provider runs it inside this same call. search_context_size is config-driven.
            openai_tools = [
                *openai_tools,
                {
                    "type": "web_search",
                    "search_context_size": settings.resolved_search_context_size(),
                },
            ]

        try:
            response = await client.responses.create(
                model=model,
                instructions=system_prompt,
                input=cast(Any, input_items),
                tools=cast(Any, openai_tools) if openai_tools else openai.omit,
                max_output_tokens=self._max_tokens,
                store=store,
                reasoning=cast(Any, reasoning),
                include=cast(Any, include),
            )
        except openai.AuthenticationError as exc:
            _log_upstream_error(exc, model=model, status_code=getattr(exc, "status_code", 401))
            raise OpenAIAuthError(str(exc)) from exc
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            _log_upstream_error(exc, model=model, status_code=None)
            raise UpstreamError("openai upstream error") from exc
        except openai.APIStatusError as exc:
            _log_upstream_error(exc, model=model, status_code=getattr(exc, "status_code", None))
            raise UpstreamError("openai upstream error") from exc

        # content_blocks = the raw output[] items for persist/replay (stored verbatim in
        # chat_steps.payload; _build_input replays them as input). model_dump(exclude_none) keeps
        # the payload clean and JSON-serializable for JSONB.
        content_blocks: list[dict[str, Any]] = []
        text_parts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        has_function_call = False
        for item in response.output:
            content_blocks.append(item.model_dump(mode="json", exclude_none=True))
            if isinstance(item, ResponseOutputMessage):
                for part in item.content:
                    if isinstance(part, ResponseOutputText):
                        text_parts.append(part.text)
            elif isinstance(item, ResponseFunctionToolCall):
                # ADR-059 §1: only function_call becomes a tool_use. web_search_call / reasoning are
                # persisted (above) but never surfaced as tool_uses and never reverse-mapped.
                has_function_call = True
                try:
                    domain_name = to_domain_tool_name(item.name)
                except UnknownToolNameError as exc:
                    # Upstream anomaly: an unmapped tool name must never surface as a valid tool.
                    raise ValidationFailedError(str(exc)) from exc
                try:
                    parsed_args = json.loads(item.arguments) if item.arguments else {}
                except (ValueError, json.JSONDecodeError) as exc:
                    raise ValidationFailedError(
                        f"invalid tool_call arguments JSON for {item.name}"
                    ) from exc
                if not isinstance(parsed_args, dict):
                    raise ValidationFailedError(
                        f"tool_call arguments for {item.name} must be a JSON object"
                    )
                # ADR-008/ADR-059 §1: correlate on call_id (call_...), NOT id (fc_...).
                tool_uses.append({"id": item.call_id, "name": domain_name, "input": parsed_args})

        return LLMResult(
            stop_reason=self._to_stop_reason(response, has_function_call=has_function_call),
            content_blocks=content_blocks,
            usage=self._parse_usage(response, model),
            text="".join(text_parts),
            tool_uses=tool_uses,
        )

    async def validate_key(self, api_key: str) -> KeyValidation:
        """Lightweight OpenAI call to validate a BYOK key (ADR-016, symmetric to Anthropic).

        Uses ``models.list`` (cheap, no generation). 401 → invalid; timeout/connection → offline;
        other status → offline; ok → valid. Never logs the key.
        """
        client = self._client.with_options(api_key=api_key)
        try:
            await client.models.list()
        except openai.AuthenticationError:
            return KeyValidation.invalid
        except (openai.APITimeoutError, openai.APIConnectionError, openai.APIStatusError):
            return KeyValidation.offline
        return KeyValidation.valid
