"""Chat Orchestrator (CO-4..CO-7): policy → generate → tool-loop → debit → audit.

Implements /chat/run and /chat/tool-result. Single source of access truth is Policy Engine
(AC-6). messageStepId is the billing idempotency key, one per user message-step, reused
across all tool-rounds and re-entry (ADR-005/006). Debit happens exactly once on the final
assistant_message (mode=credits). BYOK plaintext key is in-memory only, never logged.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import (
    EVENT_CHAT_STEP,
    EVENT_POLICY_DECISION,
    EVENT_TOOL_CALL_COMPLETED,
    EVENT_TOOL_CALL_INITIATED,
    EVENT_TOOL_MUTATION,
    AuditEvent,
    AuditService,
)
from app.byok.service import BYOKService
from app.chat.anthropic_client import AnthropicAuthError
from app.chat.attachments import PreparedAttachments, prepare_attachments
from app.chat.ephemeral_repository import EphemeralChatRepository
from app.chat.global_tools import GlobalToolHandlers
from app.chat.image_client import (
    GeneratedImageData,
    ImageContentPolicyError,
    ImageGenerationError,
)
from app.chat.image_sweep import maybe_sweep_expired_images
from app.chat.llm_client import (
    STOP_REASON_MAX_TOKENS,
    STOP_REASON_TOOL_USE,
    GenerationOptions,
    LLMClient,
    LLMResult,
    NeutralMessage,
    llm_client_for,
)
from app.chat.openai_client import OpenAIAuthError
from app.chat.repository import ChatRepository, derive_title
from app.chat.tools import (
    GLOBAL_SERVER_SIDE_TOOLS,
    MUTATING_TOOLS,
    SERVER_SIDE_TOOLS,
    TOOL_IMAGE_GENERATE,
    TOOL_QUIZ_GENERATE,
    neutral_tool_definitions,
    validate_tool_args,
)
from app.config import get_settings
from app.errors import (
    InsufficientCreditsError,
    MessageNotFoundError,
    NotFoundError,
    UnsupportedDialogModeError,
    UnsupportedModelError,
    UpstreamError,
    ValidationFailedError,
    WorkspaceNotFoundError,
)
from app.models import DIALOG_MODE, ChatSession, ChatStep, GeneratedImage, ToolCall
from app.observability.logging import log_event
from app.observability.metrics import (
    blocked_requests_total,
    byok_usage_share,
    image_generation_errors_total,
    token_usage_total,
)
from app.policy.engine import (
    BlockReason,
    Decision,
    Mode,
    PolicyState,
    SubscriptionStatus,
    evaluate,
)
from app.policy.loader import load_policy_state
from app.preferences.service import PreferencesService
from app.schemas.chat import AttachmentIn, TemporaryTurn
from app.wallet.service import WalletService
from app.website.tools import SiteToolHandlers, ToolExecution
from app.workspaces.repository import WorkspacesRepository
from app.workspaces.service import WorkspacesService

logger = logging.getLogger("app.chat.orchestrator")

# ADR-028 Решение 2: hard cap for serverTools[].summary (same value as steps-view summary).
# The summary is a COMPACT indicator only — it MUST NOT carry the raw tool result, paths, URLs,
# preview signed-tokens or any secret. Anything longer is truncated to this length.
_SUMMARY_MAX_CHARS = 120

# ADR-026 §7: static, date-FREE instruction telling Claude it has no built-in knowledge of the
# current date/time and must call the time.now tool. Identical in both modes. It is STATIC (no date
# is ever interpolated), so the system prompt stays stable between requests and the Anthropic prompt
# cache (cache_control: ephemeral) is NOT invalidated — the date arrives only in the time.now
# tool_result, outside the cached system prefix.
_TIME_NOW_INSTRUCTION = (
    "You do not have built-in knowledge of the current date or time. If the user's request "
    "depends on the current date, time, or day of the week, call the time.now tool to get it; "
    "do not guess."
)

# ADR-012: base system prompt selected by assistant_mode (chat vs code). Single source of truth
# for each mode's prompt (no scattered hardcoding). The set of tools offered to Claude is
# unchanged in this sprint (Q-012-1 default deferred); only the system prompt varies.
_SYSTEM_PROMPT_CHAT = (
    "You are a helpful assistant integrated into an iOS app. You can call tools that the "
    "user's device executes locally (files, calendar, reminders). Use tools when needed and "
    "respond concisely. " + _TIME_NOW_INSTRUCTION
)
_SYSTEM_PROMPT_CODE = (
    "You are a coding assistant integrated into an iOS app. Favor precise, technical answers: "
    "produce correct, idiomatic code with brief explanations. You can call tools that the "
    "user's device executes locally (files, calendar, reminders) and server-side site tools. "
    "Use tools when needed and respond concisely. " + _TIME_NOW_INSTRUCTION
)


def _system_prompt_for(assistant_mode: str) -> str:
    return _SYSTEM_PROMPT_CODE if assistant_mode == "code" else _SYSTEM_PROMPT_CHAT


# ADR-037 §1,§3: allowlist for ChatRunRequest.context — a fixed registry of known per-message
# conversation settings, rendered into a compact text block prepended to the turn-0 user message.
# The rendered key order is FIXED (the order below), independent of the request dict's key order
# (deterministic block). Unknown keys are ignored (forward-compat); a key whose value fails its
# per-key validation is dropped (lenient, NOT a 422). Free-string keys have a length cap; enum keys
# must match a closed set; locale additionally enforces a character class. The whole context block
# is INJECTED INTO THE USER MESSAGE (never the system prompt) — so the Anthropic prompt cache
# (cache_control: ephemeral on system) is not invalidated and user data does not gain system
# authority (05-security.md).
_CONTEXT_FREE_STRING_MAX = {
    "codeLanguage": 40,
    "tone": 40,
    "locale": 35,
}
_CONTEXT_ENUMS = {
    "responseStyle": frozenset({"concise", "balanced", "detailed"}),
    "verbosity": frozenset({"low", "medium", "high"}),
}
# locale: BCP-47-like, restricted character class to keep arbitrary text out of the block (§1).
_CONTEXT_LOCALE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
# Deterministic render order (ADR-037 §3 = the allowlist-table order).
_CONTEXT_KEY_ORDER = ("codeLanguage", "responseStyle", "verbosity", "tone", "locale")


def _sanitize_context_value(value: str) -> str:
    """Strip block-structure characters from a free-string value (ADR-037 §3 escaping).

    Newlines / ``;`` / ``=`` would break the single-line ``k=v; k=v`` block structure, so they are
    replaced with a space and the result is collapsed/stripped. Defensive against a value smuggling
    its own delimiters into the conversation-settings block.
    """
    cleaned = value.replace("\n", " ").replace("\r", " ").replace(";", " ").replace("=", " ")
    return " ".join(cleaned.split())


def _validated_context_value(key: str, raw: Any) -> str | None:
    """Validate+normalize one context value for ``key`` per ADR-037 §1; None → drop the key.

    All values must be ``str`` and non-empty after ``strip``. Free-string keys are length-capped
    (chars, post-strip) then sanitized; enum keys are lower-cased and must be in the closed set;
    ``locale`` must match the restricted character class. A wrong type / out-of-range / out-of-enum
    value yields None (the key is ignored — lenient, never a 422).
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if key in _CONTEXT_ENUMS:
        lowered = value.lower()
        return lowered if lowered in _CONTEXT_ENUMS[key] else None
    if key == "locale":
        if len(value) > _CONTEXT_FREE_STRING_MAX["locale"]:
            return None
        if any(ch not in _CONTEXT_LOCALE_CHARS for ch in value):
            return None
        return value  # already constrained to a safe char class; no sanitize needed
    if key in _CONTEXT_FREE_STRING_MAX:
        if len(value) > _CONTEXT_FREE_STRING_MAX[key]:
            return None
        sanitized = _sanitize_context_value(value)
        return sanitized or None
    # Unknown key (not in any allowlist branch) → ignored by the caller's iteration over the fixed
    # key order; this path is unreachable for keys in _CONTEXT_KEY_ORDER. Defensive None.
    return None  # pragma: no cover


def _render_context_block(context: dict[str, Any] | None) -> str | None:
    """Render the deterministic per-message conversation-settings block (ADR-037 §3).

    Returns None when ``context`` is absent/empty or no allowlisted key survives validation (→ the
    turn behaves exactly as without ``context``). Otherwise returns a single-line block in FIXED key
    order with only the valid present keys, e.g.::

        [Conversation settings for this message: codeLanguage=Swift; responseStyle=concise]

    Unknown keys are ignored; per-key-invalid values are dropped (lenient). The content of
    ``context`` is NEVER logged (05-security.md) — this function neither logs nor raises.
    """
    if not context:
        return None
    parts: list[str] = []
    for key in _CONTEXT_KEY_ORDER:
        if key not in context:
            continue
        value = _validated_context_value(key, context[key])
        if value is not None:
            parts.append(f"{key}={value}")
    if not parts:
        return None
    return f"[Conversation settings for this message: {'; '.join(parts)}]"


def _compose_turn0_text(block: str | None, msg: str) -> str:
    """Compose the turn-0 user text from the context block (ADR-037) and the message (ADR-039 §3).

    Returns "" only when there is no text at all (empty/whitespace-only message AND no context
    block) — the caller then omits the text block entirely (image-only / file-only turn, §2). A
    whitespace-only message is treated as «no text» (``.strip()``, symmetric with the §1 validator)
    so a blank text block is never sent to the provider. No trailing ``"\\n\\n"`` is produced when
    the message is empty but a block is present.
    """
    if not msg.strip():
        return block or ""
    if block is not None:
        return f"{block}\n\n{msg}"
    return msg


# ADR-055 §4: dialog modes gated to an OpenAI instance (Responses API mechanisms — reasoning /
# web_search / strict quiz-tool). `smart` is provider-agnostic (works on Anthropic too).
_PROVIDER_GATED_DIALOG_MODES = frozenset({"deep_thinking", "study_learn", "search"})

# ADR-055 §5: per-mode system-prompt suffix, appended AFTER the base assistant_mode prompt and any
# workspace instructions (does not break the prompt structure / injection order). Each suffix is
# STATIC (deterministic) so the provider prompt cache is not invalidated. `smart`/`deep_thinking`
# carry no suffix here: smart is unchanged, and deep_thinking is driven by `reasoning.effort` on the
# provider side (ADR-059 §3), not by prompt text. `study_learn` gets the learning suffix (this
# sprint the quiz.generate tool is deferred — ADR-057/sprint 4 — so the suffix is its ONLY effect);
# `search` gets a suffix nudging the model to use the built-in web_search tool and cite sources.
_DIALOG_MODE_PROMPT_SUFFIX: dict[str, str] = {
    "study_learn": (
        "You are in Study & Learn mode. Teach the user: explain concepts step by step in a clear, "
        "didactic style, check understanding, and encourage active recall. Keep explanations "
        "accurate and concise."
    ),
    "search": (
        "You are in Search mode. When the user's request depends on current, factual, or "
        "external information, use the web_search tool and ground your answer in the results. "
        "Cite the sources you relied on."
    ),
}


def _system_prompt_with_dialog_mode(base_prompt: str, dialog_mode: str) -> str:
    """Append the dialog-mode suffix AFTER the base (+ workspace) prompt (ADR-055 §5).

    ``base_prompt`` is the already-composed prompt (base assistant_mode prompt, optionally with the
    workspace instructions). A per-mode static suffix is appended for modes that define one
    (study_learn / search); modes without a suffix (smart / deep_thinking) return the base unchanged
    so the prompt cache is not broken. Deterministic — the suffix depends only on the mode.
    """
    suffix = _DIALOG_MODE_PROMPT_SUFFIX.get(dialog_mode)
    if suffix:
        return f"{base_prompt}\n\n{suffix}"
    return base_prompt


def _system_prompt_with_workspace(assistant_mode: str, instructions: str | None) -> str:
    """Compose the system prompt for a workspace session (ADR-036 §3).

    ``base(assistant_mode)`` → ``\\n\\n`` → ``workspace.instructions`` when instructions are
    non-empty; otherwise the base prompt unchanged (so the prompt cache is not broken for sessions
    without instructions). Provider-agnostic (part of ``system``, identical for both providers).
    """
    base = _system_prompt_for(assistant_mode)
    if instructions and instructions.strip():
        return f"{base}\n\n{instructions.strip()}"
    return base


def _merge_attachments(
    chat: PreparedAttachments | None, workspace: PreparedAttachments | None
) -> PreparedAttachments | None:
    """Merge workspace knowledge-file blocks with the request's inline attachment blocks (ADR-036).

    Both are injected into the last user turn on the first call only. Workspace context blocks are
    placed BEFORE the request attachments (project context first). placeholders come only from the
    request attachments (workspace files are never persisted as user-step placeholders — they are
    re-assembled from workspace_files on a new session's first turn).
    """
    if chat is None and workspace is None:
        return None
    chat_blocks = chat.content_blocks if chat is not None else []
    chat_placeholders = chat.placeholders if chat is not None else []
    ws_blocks = workspace.content_blocks if workspace is not None else []
    return PreparedAttachments(
        content_blocks=[*ws_blocks, *chat_blocks],
        placeholders=list(chat_placeholders),
    )


def _active_provider() -> str:
    """Active LLM provider (ADR-033) for provider-aware attachment validation. Default anthropic."""
    return get_settings().llm_provider.strip().lower()


def _model_for_provider(model: str | None, provider: str) -> str | None:
    """Return ``model`` only if it is in ``provider``'s allowlist, else ``None`` (ADR-044).

    Shared stale-model guard for both billing modes:
    - credits (ADR-044 §Связанное / orchestrator §Stale-model): ``provider`` = the ACTIVE instance
      provider. A session model fixed for another provider (e.g. ``claude-*`` after the instance was
      switched to ``LLM_PROVIDER=openai``) is NOT in the active allowlist → ``None`` → the client
      uses its provider default instead of failing with ``create_message(model=foreign)``.
    - byok (ADR-044 §5.3): ``provider`` = the KEY's provider. A session model of another provider is
      never forwarded to the key's client.

    ``model is None`` (instance default) stays ``None``. The DB ``chat_sessions.model`` is never
    rewritten — only the value passed to the client on this call changes (expand-only, ADR-034).
    """
    if model is None:
        return None
    return model if model in get_settings().allowed_models_for(provider) else None


def _server_tool_summary(execution: ToolExecution) -> str | None:
    """Build the COMPACT serverTools[].summary for a server-side execution (ADR-028 Решение 2).

    MVP default (Q-028-1): a single compact summary, NOT the raw result. completed → "ok";
    errored → the short machine error code (e.g. "invalid_timezone"), never details/stacktraces.
    The raw result/path/URL/signed-token NEVER appears here (it stays only in /chats history,
    ADR-024). Defensively truncated to _SUMMARY_MAX_CHARS even though codes are already short.
    """
    if execution.is_error:
        code = execution.error_code or "errored"
        return code[:_SUMMARY_MAX_CHARS]
    return "ok"


@dataclass(frozen=True)
class ToolCallOut:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ServerToolExecutionOut:
    """One server-side tool execution of this /chat/run call (ADR-028 Решение 2).

    tool_name is the DOMAIN dotted name (anthropic_client already reverse-maps tool_use.name to
    domain before it reaches the orchestrator). summary is a COMPACT, already-truncated indicator
    (≤ _SUMMARY_MAX_CHARS) and NEVER the raw result / path / URL / signed-token.

    tool_call_id is the DOMAIN tool_calls.id (uuid4) of this server-side execution (ADR-030).
    It equals the toolCallId of the matching tool step in GET /v1/chats/{id} (correlation
    invariant) and is the same id domain as client-side toolCalls[].id — NOT the provider
    toolu_... id (ADR-008).
    """

    tool_call_id: uuid.UUID
    tool_name: str
    status: str  # completed | errored
    summary: str | None


@dataclass(frozen=True)
class ToolResultIn:
    """One normalized tool-result item (ADR-025 batch). error is the dumped ToolErrorBody dict."""

    tool_call_id: uuid.UUID
    result: dict[str, Any] | None
    error: dict[str, Any] | None


@dataclass(frozen=True)
class GeneratedImageOut:
    """One image generated by image.generate in this turn (ADR-058) → ChatResponse.images[].

    Carries ONLY the id + metadata (bytes are fetched via GET /v1/images/{id}); the prompt is NOT
    included (TD-035). Accumulated append-all (a turn may generate several images).
    """

    image_id: uuid.UUID
    content_type: str
    size: int


@dataclass(frozen=True)
class _ImagePregen:
    """Pre-generated outcome for ONE image.generate block (ADR-058, MAJOR-4 — connection safety).

    The bytes are generated BEFORE any DB write so a pooled connection is NOT held during the slow
    external images.generate call. Holds EITHER the generated ``data`` OR the caught ``error`` (a
    degrade — content-policy or other generation failure). The base ``ImageGenerationError`` is
    stored verbatim; it is classified later by ``isinstance`` in the correct order
    (``ImageContentPolicyError`` first) where the domain ``tool_call_id`` exists for the TD-035 log.
    """

    data: GeneratedImageData | None
    error: ImageGenerationError | None


@dataclass(frozen=True)
class ChatRunOut:
    status: str  # assistant_message | tool_call | blocked
    session_id: uuid.UUID
    assistant_message: str | None = None
    # ADR-025: ALL client-side tool calls of the turn (parallel tool use). tool_call (singular,
    # deprecated) = tool_calls[0]. Server-side site.* are executed on the backend and excluded.
    tool_calls: list[ToolCallOut] | None = None
    tool_call: ToolCallOut | None = None
    block_reason: str | None = None
    usage: dict[str, Any] | None = None
    # ADR-023: sync ids for chat history. message_step_id = the turn (one per user message-step,
    # reused across tool-rounds/re-entry); step_id = the id of the persisted assistant/tool step
    # this response represents (= ChatStep.id = ChatStepSchema.id). Both None for policy-blocked
    # (no step/turn is created — policy blocks before generation). For blocked+max_tokens (ADR-025)
    # both are set (the truncated assistant step IS created) and usage is present.
    message_step_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    # ADR-028 Решение 2: server-side tools (site.* / time.now) executed by the backend during THIS
    # /chat/run (or one /chat/tool-result continuation), in execution order. Always a list (possibly
    # empty). Empty for policy-blocked (tool-loop never ran); may be NON-empty for
    # blocked+max_tokens (server-side rounds could run before the final turn was truncated).
    server_tools: list[ServerToolExecutionOut] = field(default_factory=list)
    # ADR-057: the Study & Learn quiz produced by quiz.generate in THIS turn (the LAST one if the
    # model emitted several), echoed as the validated {question, options, correctIndex, explanation}
    # dict → ChatResponse.quiz. None unless dialog_mode == study_learn AND the model called
    # quiz.generate. The tool is global server-side, so this works in a temporary chat too (no DB).
    quiz: dict[str, Any] | None = None
    # ADR-058: images generated by image.generate in THIS turn (append-all, NOT last-wins — a turn
    # may produce several). None when no image was generated. Threaded like server_tools/quiz into
    # every terminal ChatRunOut so the client sees them regardless of how the turn ended.
    images: list[GeneratedImageOut] | None = None


@dataclass(frozen=True)
class _TurnOutcome:
    """Result of processing one tool_use turn (ADR-011).

    client_out is set when the turn yields a client-side tool_call to hand off to iOS; None when
    the turn was purely server-side (site.*) and the orchestrator should continue the loop.
    blocked_out (ADR-058 §5) is set when an image.generate debit hit insufficient credits: the
    handler already rolled the turn back (bytes not saved), and the loop returns this blocked
    ChatRunOut WITHOUT committing.
    """

    client_out: ChatRunOut | None
    blocked_out: ChatRunOut | None = None


@dataclass(frozen=True)
class _BillingPlan:
    """How the final assistant_message must be billed (ADR-002 + ADR-005).

    Exactly one of the two flags is true when billing applies:
    - debit_credits: active subscription + mode=credits → consume 1 credit (idempotent).
    - mark_trial:    subscription=none + trial_used=false + mode=credits → free trial, flip
      users.trial_used (idempotent). No debit.
    BYOK and trial generations are free → both flags false.
    """

    debit_credits: bool
    mark_trial: bool


def _billing_plan(mode: Mode, state: PolicyState) -> _BillingPlan:
    if mode is Mode.byok:
        return _BillingPlan(debit_credits=False, mark_trial=False)
    # mode == credits
    if state.subscription_status is SubscriptionStatus.active:
        # ADR-002: "active + credits>0 → allow + debit". Only here do we charge a credit.
        return _BillingPlan(debit_credits=True, mark_trial=False)
    if state.subscription_status is SubscriptionStatus.none and not state.trial_used:
        # ADR-002: trial-allow has NO debit; instead the lifetime trial is consumed.
        return _BillingPlan(debit_credits=False, mark_trial=True)
    # Any other credits state would have been blocked by policy before reaching here.
    return _BillingPlan(debit_credits=False, mark_trial=False)


@dataclass
class _Deps:
    repo: ChatRepository
    wallet: WalletService
    byok: BYOKService
    audit: AuditService
    # ADR-033: provider-neutral LLM client (AnthropicClient | OpenAIClient). The orchestrator
    # depends only on the LLMClient contract and neutral types — never on a concrete provider.
    llm: LLMClient
    site_tools: SiteToolHandlers
    # ADR-026: project-independent global server-side tools (time.now), executed without a project.
    global_tools: GlobalToolHandlers
    preferences: PreferencesService
    # ADR-036: workspaces context provider (instructions + knowledge files) for workspace chats.
    workspaces: WorkspacesService


class ChatOrchestrator:
    def __init__(
        self,
        session: AsyncSession,
        repo: ChatRepository,
        wallet: WalletService,
        byok: BYOKService,
        audit: AuditService,
        anthropic_client: LLMClient,
        site_tools: SiteToolHandlers,
        preferences: PreferencesService,
        global_tools: GlobalToolHandlers | None = None,
        workspaces: WorkspacesService | None = None,
    ) -> None:
        self._session = session
        # ADR-056: set True for a temporary-chat run (repo swapped to EphemeralChatRepository). It
        # is per-request state (a fresh orchestrator is built per request via get_orchestrator) and
        # gates the FK-bearing session id for audit/wallet writes (see _fk_session_id). tool_result
        # never runs a temporary turn, so it stays False there.
        self._temporary = False
        self._deps = _Deps(
            repo=repo,
            wallet=wallet,
            byok=byok,
            audit=audit,
            # ADR-033: the injected client is the active provider's LLMClient. The param name is
            # kept (anthropic_client) for caller backward compatibility; the field is provider-
            # neutral (`llm`).
            llm=anthropic_client,
            site_tools=site_tools,
            # Default to a SystemClock-backed handler so existing callers keep working; the DI
            # factory (deps.py) wires an explicit instance (ADR-026 §5).
            global_tools=global_tools if global_tools is not None else GlobalToolHandlers(),
            preferences=preferences,
            # ADR-036: default to a session-backed WorkspacesService so existing callers keep
            # working; the DI factory (deps.py) wires the same instance explicitly.
            workspaces=(
                workspaces
                if workspaces is not None
                else WorkspacesService(WorkspacesRepository(session))
            ),
        )

    # ---- public entrypoints ----

    async def run(
        self,
        *,
        user_id: uuid.UUID,
        project_id: str | None,
        session_id: uuid.UUID | None,
        message: str,
        mode: str,
        assistant_mode: str | None = None,
        dialog_mode: str | None = None,
        attachments: list[AttachmentIn] | None = None,
        model: str | None = None,
        workspace_project_id: uuid.UUID | None = None,
        context: dict[str, Any] | None = None,
        edit_message_step_id: uuid.UUID | None = None,
        temporary: bool = False,
        history: list[TemporaryTurn] | None = None,
    ) -> ChatRunOut:
        message_step_id = uuid.uuid4()  # CO-4b: billing key for this user message-step
        # ADR-056: a temporary chat persists NOTHING. Swap the repo to an in-memory implementation
        # BEFORE any repo use (get_or_create_session / add_step below) so no chat-* row is written;
        # the invariant «only ChatRepository writes chat-* tables» (ADR-021) holds because the
        # persisting repo does not run in this path. The schema guarantees temporary is incompatible
        # with sessionId/editMessageStepId/projectId/workspaceProjectId, so this run always creates
        # a fresh synthetic session. `self._temporary` gates the FK session id for audit/wallet.
        if temporary:
            self._temporary = True
            self._deps.repo = EphemeralChatRepository(
                self._session, seed=history or [], provider=_active_provider()
            )
        # ADR-034 §3: resolve the session-fixed model. None (no field) → NULL (= instance default,
        # never substituted in the DB so the row stays "instance default" even if env default
        # changes). The schema guarantees a non-empty value here, so .strip() is safe.
        resolved_model = model.strip() if model is not None else None
        # ADR-034 §3: validate allowlist membership ONLY when a NEW session is being created (a
        # missing session_id, or an absent/expired one → get_or_create_session creates). On resume
        # the request `model` is IGNORED (the stored model is already valid) — so a bad model field
        # on a resume must NOT fail. Pre-determine «is this a create?» to gate validation; the
        # validation itself runs BEFORE the session row is created (no invalid model is written).
        will_create = await self._will_create_session(user_id, session_id)
        if (
            will_create
            and resolved_model is not None
            and resolved_model not in get_settings().allowed_models()
        ):
            raise UnsupportedModelError(
                f"model '{resolved_model}' is not available on this instance"
            )
        # ADR-036 §3: workspaceProjectId is session-fixed (like mode/model). On CREATE validate the
        # workspace belongs to the user (foreign/missing → 404 workspace_not_found, isolation)
        # BEFORE the session row is written; on resume the request field is ignored (the binding is
        # read from the session). Empty/None → a chat without a workspace (backward-compatible).
        if (
            will_create
            and workspace_project_id is not None
            and not await self._deps.workspaces.owns_workspace(workspace_project_id, user_id)
        ):
            raise WorkspaceNotFoundError("workspace not found")
        # ADR-012: resolve assistant_mode for a NEW session — explicit request → preferences
        # default → 'chat'. Fixed on the session at creation; ignored when resuming a session
        # (assistant_mode is a session attribute). billing_mode (`mode`) is independent.
        resolved_assistant_mode = (
            assistant_mode
            if assistant_mode is not None
            else await self._deps.preferences.get_default_assistant_mode(user_id)
        )
        # ADR-055 §2,§3: resolve the session-fixed dialog_mode for a NEW session — explicit request
        # → preferences default (default_dialog_mode) → 'smart' (copy of resolved_assistant_mode's
        # mechanics). Fixed on the session at creation; ignored when resuming (read from the
        # session). Validation (membership + provider-gate) runs ONLY on create, BEFORE the row is
        # written, so a bad dialogMode on resume must NOT fail and no invalid value is persisted.
        resolved_dialog_mode = (
            dialog_mode
            if dialog_mode is not None
            else await self._deps.preferences.get_default_dialog_mode(user_id)
        )
        if will_create:
            # (a) membership: the request field is a free `str` (not a Literal), so an unknown value
            # must be rejected with the machine code, not a generic 422 (ADR-055 §6).
            if resolved_dialog_mode not in DIALOG_MODE:
                raise UnsupportedDialogModeError(
                    f"dialog mode '{resolved_dialog_mode}' is not supported"
                )
            # (b) provider-gate: deep_thinking/study_learn/search require an active OpenAI provider
            # (ADR-055 §4 / ADR-059). Not a business block → 422, NOT `blocked` (ADR-004).
            if (
                resolved_dialog_mode in _PROVIDER_GATED_DIALOG_MODES
                and _active_provider() != "openai"
            ):
                raise UnsupportedDialogModeError(
                    f"dialog mode '{resolved_dialog_mode}' is not available on this instance"
                )
        ctx = await self._deps.repo.get_or_create_session(
            user_id=user_id,
            project_id=project_id,
            mode=mode,
            session_id=session_id,
            assistant_mode=resolved_assistant_mode,
            # ADR-055 §2: session-fixed dialog mode; written only at creation, ignored on resume.
            dialog_mode=resolved_dialog_mode,
            # Auto-title from the first user message (chats/03); only used for a new session.
            title=derive_title(message),
            # ADR-034 §3: session-fixed model; written only at creation, ignored on resume.
            model=resolved_model,
            # ADR-036 §3: session-fixed workspace binding; written only at creation, ignored on
            # resume (the request field is validated above only when a new session is created).
            workspace_project_id=workspace_project_id if will_create else None,
        )
        sess = ctx.session
        # mode is fixed on the session; use the session's stored mode.
        effective_mode = Mode(sess.mode)

        # ADR-040 §2,§3: edit+regenerate. Truncate the session history from the edited turn (its
        # user-step and EVERYTHING after) BEFORE persisting the new user-step of this turn, in the
        # same request transaction (atomic; the request commits as one unit). Edit REQUIRES resume
        # of an existing OWNED session (ADR-040 §1,§5): a new session (sessionId was given but the
        # session is foreign/expired/missing → get_or_create created a fresh one, ctx.is_new=True)
        # means there is no turn to edit → 404, NO truncation, and the empty just-created session
        # row is rolled back with the request (the AppError propagates → db.session_scope rollback;
        # commit happens only on success). Truncation is scoped by `sess.id` — the resumed, owned
        # session — so a foreign chat can never be truncated. The new turn then proceeds normally:
        # the freshly generated message_step_id (above) yields a new debit (CO-7); on resume
        # (is_new=False) workspace files are NOT re-injected (turn-0-only, ADR-040 §4а).
        if edit_message_step_id is not None:
            if ctx.is_new:
                raise MessageNotFoundError("message_not_found")
            deleted = await self._deps.repo.truncate_from_message_step(
                sess.id, edit_message_step_id
            )
            if deleted is None:
                raise MessageNotFoundError("message_not_found")

        # ADR-036 §3/§6 + ADR-038 §3: workspace `instructions` live in the `system` param (NOT in
        # history) and MUST be injected on EVERY turn of a session with a workspace — decoupled
        # from `ctx.is_new` so that a chat MOVED into a workspace later (PATCH, ADR-038) also gets
        # the project instructions from its next message. Knowledge FILES stay turn-0-only (ADR-038
        # §3.2, variant a): they are heavy user-content, persisted as history content blocks on
        # turn 0 and replayed automatically; NOT re-injected retroactively for a moved chat
        # (Q-038-1).
        #   - turn 0 (new session): assemble (instructions + files) via context_for_session;
        #   - resume/next turn (not is_new): read ONLY instructions via instructions_for_session
        #     (light single-column) — files are NOT collected (context_for_session is not called).
        # For a non-workspace chat the system prompt is unchanged (base) → no double-injection and
        # the provider prompt cache stays intact.
        workspace_attachments: PreparedAttachments | None = None
        system_prompt = _system_prompt_for(sess.assistant_mode)
        if sess.workspace_project_id is not None:
            if ctx.is_new:
                ws_context = await self._deps.workspaces.context_for_session(
                    sess.workspace_project_id, user_id, provider=_active_provider()
                )
                if ws_context is not None:
                    system_prompt = _system_prompt_with_workspace(
                        sess.assistant_mode, ws_context.instructions
                    )
                    workspace_attachments = ws_context.attachments
            else:
                instructions = await self._deps.workspaces.instructions_for_session(
                    sess.workspace_project_id, user_id
                )
                system_prompt = _system_prompt_with_workspace(sess.assistant_mode, instructions)
        # ADR-055 §5: append the dialog-mode suffix AFTER the base assistant_mode prompt and any
        # workspace instructions (composition order preserved). No-op for smart/deep_thinking.
        system_prompt = _system_prompt_with_dialog_mode(system_prompt, sess.dialog_mode)

        # ADR-020 / ADR-033 §3,§5: validate inline attachments (provider-aware) and split into
        # (a) the PreparedAttachments handed to the client ONCE on turn 0 — the client builds the
        # provider content blocks and injects them — and (b) light text placeholders persisted in
        # chat_steps.payload (provider-agnostic). Raw base64 is NEVER persisted (storage invariant).
        # Validation runs BEFORE persisting the user step so a bad attachment (incl. PDF-on-OpenAI)
        # is a clean 422 with no DB write. The shared validation runs before the provider branch.
        # ADR-037 §3,§4: build the per-message conversation-settings block from `context` and
        # PREPEND it to the turn-0 user text (block leads, then "\n\n", then the user message). When
        # no valid key survives validation → None → the text is the bare message (unchanged). The
        # block is injected into the USER content here — the single common turn-0 assembly point
        # BEFORE the provider client — never into `system` (prompt-cache invariant, ADR-037 §5) and
        # provider-agnostically (plain text in user content works on both Anthropic and OpenAI). It
        # is part of the persisted user-step payload below → correct replay; on continuation /
        # tool-result it is NOT re-injected (it already lives in the history of this turn).
        # ADR-039 §2,§3: compose the turn-0 user text (context block + message) and add the text
        # block ONLY when the text is non-empty. For an image-only / file-only turn the text is ""
        # and NO text block is created — a blank text block (text="") is never sent to the provider
        # (Anthropic/OpenAI may reject it; the decision lives here, the single turn-0 assembly
        # point, not in the clients). The validator (§1) guarantees the resulting content is
        # non-empty: empty text ⇒ there is ≥1 attachment ⇒ ≥1 placeholder. Text block (if any)
        # leads, then the attachment placeholders — order unchanged.
        context_block = _render_context_block(context)
        message_text = _compose_turn0_text(context_block, message)
        prepared: PreparedAttachments | None = None
        if attachments:
            prepared = prepare_attachments(attachments, get_settings(), _active_provider())
        text_blocks: list[dict[str, Any]] = (
            [{"type": "text", "text": message_text}] if message_text else []
        )
        placeholders = prepared.placeholders if prepared is not None else []
        user_payload_content: list[dict[str, Any]] = [*text_blocks, *placeholders]

        # ADR-036 §6: merge the workspace knowledge-file blocks with the request's inline
        # attachment blocks (project context first). Only the request attachments leave a persisted
        # placeholder; workspace files are re-assembled from workspace_files, never persisted here.
        first_turn = _merge_attachments(prepared, workspace_attachments)

        # Persist the user message under this step (placeholders only — no base64, ADR-020 §3).
        await self._deps.repo.add_step(
            session_id=sess.id,
            message_step_id=message_step_id,
            role="user",
            payload={"content": user_payload_content},
        )

        decision, state = await self._evaluate(user_id, effective_mode, sess.id)
        if not decision_allow(decision):
            return self._blocked(sess.id, decision.block_reason)

        # mode=byok: resolve plaintext key in-memory + its provider (CO-6, ADR-044 §5).
        api_key, byok_provider = await self._resolve_api_key(user_id, effective_mode)

        result = await self._generate_loop(
            user_id=user_id,
            session_id=sess.id,
            message_step_id=message_step_id,
            mode=effective_mode,
            billing=_billing_plan(effective_mode, state),
            api_key=api_key,
            byok_provider=byok_provider,
            system_prompt=system_prompt,
            # ADR-022 axis A: offer site.* only when the session has a project.
            has_project=sess.project_id is not None,
            first_turn_attachments=first_turn,
            # ADR-034 §4 / ADR-044: session-fixed model (NULL → None). The effective model is
            # resolved inside _generate_loop against the right provider's allowlist (stale-model
            # fallback): credits → active provider, byok → key provider.
            model=sess.model or None,
            # ADR-055 §5: session-fixed dialog mode drives generation (deep_thinking forces the
            # reasoning model; the mode is forwarded to the client via GenerationOptions).
            dialog_mode=sess.dialog_mode,
            # ADR-056: temporary run → client-side tools off + store=False; forwarded to the loop.
            temporary=temporary,
        )
        await self._sweep_if_images(result)
        return result

    async def tool_result(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        results: list[ToolResultIn],
    ) -> ChatRunOut:
        """Apply a batch of tool results and continue only when the turn barrier closes (ADR-025).

        Each item is applied independently (per-item idempotency). The continuation to Anthropic is
        gated by the turn barrier: it runs ONLY when every client-side tool_call of the assistant
        turn (one message_step_id) is completed/errored — otherwise an orphan tool_use would make
        Anthropic reject the next messages.create (400 → 502). Until the barrier closes the response
        is status=tool_call with the remaining (not-yet-completed) client-side calls.
        """
        if not results:  # pragma: no cover - schema guarantees non-empty
            raise ValidationFailedError("results must be non-empty")

        # Resolve every referenced tool_call; enforce session ownership + single-turn invariant.
        sess = await self._deps.repo.get_session(session_id, user_id)
        if sess is None:
            raise NotFoundError("session not found")

        resolved: list[tuple[ToolResultIn, ToolCall]] = []
        message_step_id: uuid.UUID | None = None
        for item in results:
            tool_call = await self._deps.repo.get_tool_call(item.tool_call_id)
            if tool_call is None or tool_call.session_id != session_id:
                raise NotFoundError("tool call not found for session")
            if message_step_id is None:
                message_step_id = tool_call.message_step_id
            elif tool_call.message_step_id != message_step_id:
                # All batch items must belong to one turn (one message_step_id) — 02-api-contracts.
                raise ValidationFailedError("all results must belong to the same turn")
            resolved.append((item, tool_call))

        assert message_step_id is not None  # noqa: S101 - results is non-empty

        # Apply each result (per-item idempotency, ADR-005): already completed/errored → skip
        # the write (do NOT overwrite, do NOT re-audit). New ones transition pending → done.
        for item, tool_call in resolved:
            if tool_call.status in ("completed", "errored"):
                continue  # idempotent: result not overwritten
            await self._apply_tool_result(
                user_id=user_id,
                session_id=session_id,
                message_step_id=message_step_id,
                tool_call=tool_call,
                result=item.result,
                error=item.error,
            )

        # ADR-025 barrier: continuation only when ALL client-side tool_calls of this turn are
        # completed/errored. Server-side tools (project-scoped site.* AND global time.now,
        # ADR-026 §4) are executed on the backend and were completed in the run loop; the barrier
        # considers only client-side calls.
        turn_calls = await self._deps.repo.list_tool_calls_for_step(session_id, message_step_id)
        client_calls = [
            tc
            for tc in turn_calls
            if tc.tool_name not in SERVER_SIDE_TOOLS
            and tc.tool_name not in GLOBAL_SERVER_SIDE_TOOLS
        ]
        pending = [tc for tc in client_calls if tc.status not in ("completed", "errored")]
        if pending:
            # Barrier not closed → tell the client which results are still awaited. No Anthropic
            # call, no billing. messageStepId stable; stepId = the assistant turn step with the
            # tool_use blocks (ADR-025: same turn).
            await self._session.commit()
            remaining = [
                ToolCallOut(id=str(tc.id), name=tc.tool_name, args=dict(tc.args)) for tc in pending
            ]
            assistant_step_id = await self._deps.repo.assistant_tool_step_id(
                session_id, message_step_id
            )
            return ChatRunOut(
                status="tool_call",
                session_id=session_id,
                tool_calls=remaining,
                tool_call=remaining[0],
                message_step_id=message_step_id,
                step_id=assistant_step_id,
            )

        # Barrier closed. Idempotent replay: if a continuation step was already saved for this turn
        # (e.g. a repeated batch after the turn completed), return it without re-calling Anthropic.
        anchor_id = resolved[0][1].id
        saved = await self._deps.repo.next_step_after(session_id, message_step_id, anchor_id)
        if saved is not None and self._all_already_done_before(resolved):
            return self._render_saved_step(session_id, message_step_id, saved)

        mode = Mode(sess.mode)
        # Re-evaluate policy (access may have changed).
        decision, state = await self._evaluate(user_id, mode, session_id)
        if not decision_allow(decision):
            return self._blocked(session_id, decision.block_reason)

        api_key, byok_provider = await self._resolve_api_key(user_id, mode)
        # ADR-036 §3: knowledge files are already replayed as content blocks in the history, but
        # `instructions` live in the `system` param (NOT in history) and are sent on EVERY LLM call.
        # So on each continuation re-inject the workspace instructions into system via the SAME
        # helper used on turn 0 (identical behavior). Read ONLY instructions (light single-column);
        # do NOT re-inject knowledge files. Empty/missing instructions or a deleted workspace → base
        # system prompt unchanged (graceful).
        system_prompt = _system_prompt_for(sess.assistant_mode)
        if sess.workspace_project_id is not None:
            instructions = await self._deps.workspaces.instructions_for_session(
                sess.workspace_project_id, user_id
            )
            system_prompt = _system_prompt_with_workspace(sess.assistant_mode, instructions)
        # ADR-055 §5: re-append the dialog-mode suffix on every continuation (same helper as run),
        # AFTER base + workspace instructions — the system prompt is rebuilt each LLM call.
        system_prompt = _system_prompt_with_dialog_mode(system_prompt, sess.dialog_mode)
        result = await self._generate_loop(
            user_id=user_id,
            session_id=session_id,
            message_step_id=message_step_id,
            mode=mode,
            billing=_billing_plan(mode, state),
            api_key=api_key,
            byok_provider=byok_provider,
            system_prompt=system_prompt,
            # ADR-022 axis A: project_id is session-fixed; gate site.* by the session's project.
            has_project=sess.project_id is not None,
            # ADR-034 §4 / ADR-044: session-fixed model; effective model resolved in _generate_loop
            # against the right provider's allowlist (credits → active, byok → key provider).
            model=sess.model or None,
            # ADR-055 §5: session-fixed dialog mode drives generation on continuation too.
            dialog_mode=sess.dialog_mode,
        )
        await self._sweep_if_images(result)
        return result

    async def _sweep_if_images(self, result: ChatRunOut) -> None:
        """Opportunistic image sweep on the GENERATION path (ADR-058 §6, MINOR — table growth only).

        ADR-058 §6 names BOTH the fetch and the generation path as sweep triggers. When this turn
        produced ≥1 image the turn transaction is already committed (the image row was persisted and
        finalize/tool_call committed), so ``self._session`` is clean — the sweep runs its OWN
        throttled DELETE + commit without touching the turn's transaction or rollback semantics. It
        is best-effort and fail-open (Redis error → skip; DB error → rollback + WARNING) so it can
        NEVER fail the generation turn. A blocked/image-less turn (result.images is None) skips it.
        Redis throttling bounds the frequency regardless of image traffic. Privacy does not depend
        on it — an expired image is already unreachable by the fetch query's expires_at cond (§2).
        """
        if result.images:
            await maybe_sweep_expired_images(self._session)

    @staticmethod
    def _all_already_done_before(resolved: list[tuple[ToolResultIn, ToolCall]]) -> bool:
        """True when every referenced tool_call was ALREADY completed/errored on entry (replay).

        A fully-replayed batch (all items previously applied) closes the barrier without any new
        transition → the saved continuation step is returned idempotently rather than re-calling
        Anthropic (ADR-025 idempotency: continuation runs once per barrier close).
        """
        return all(tc.status in ("completed", "errored") for _, tc in resolved)

    async def _apply_tool_result(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_call: ToolCall,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> None:
        """Atomically transition one tool_call and persist its tool_result + audit (ADR-025)."""
        status = "errored" if error is not None else "completed"
        transitioned = await self._deps.repo.complete_tool_call(
            tool_call_id=tool_call.id,
            status=status,
            result=result if result is not None else error,
        )
        if not transitioned:
            # Concurrent completion won the race → behave idempotently (no duplicate step/audit).
            return

        # Persist the tool_result as a tool step. (result size limit is enforced at the schema
        # layer; result content is opaque per-tool and forwarded to Claude as-is.)
        await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="tool",
            payload={
                "toolCallId": str(tool_call.id),
                # ADR-008: tool_result.tool_use_id MUST equal the raw provider id of the matching
                # tool_use block, NOT the domain UUID. Stored here so _build_messages replays the
                # continuation history with a consistent id pair.
                "providerToolUseId": tool_call.provider_tool_use_id,
                "toolName": tool_call.tool_name,
                "result": result,
                "error": error,
            },
        )

        # Audit mutating tool completion (AC-7).
        if tool_call.tool_name in MUTATING_TOOLS:
            await self._deps.audit.record(
                AuditEvent(
                    user_id=user_id,
                    session_id=self._fk_session_id(session_id),
                    event_type=EVENT_TOOL_MUTATION,
                    payload={
                        "toolCallId": str(tool_call.id),
                        "toolName": tool_call.tool_name,
                        "status": status,
                    },
                )
            )
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=self._fk_session_id(session_id),
                event_type=EVENT_TOOL_CALL_COMPLETED,
                payload={
                    "toolCallId": str(tool_call.id),
                    "toolName": tool_call.tool_name,
                    "status": status,
                },
            )
        )

    # ---- internals ----

    async def _evaluate(
        self, user_id: uuid.UUID, mode: Mode, session_id: uuid.UUID
    ) -> tuple[Decision, PolicyState]:
        state = await load_policy_state(self._session, user_id)
        decision = evaluate(state, mode)
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=self._fk_session_id(session_id),
                event_type=EVENT_POLICY_DECISION,
                payload={
                    "mode": mode.value,
                    "decision": "allow" if decision.allow else "blocked",
                    "blockReason": decision.block_reason.value if decision.block_reason else None,
                },
            )
        )
        log_event(
            logger,
            logging.INFO,
            "policy_decision",
            mode=mode.value,
            allow=decision.allow,
            blockReason=decision.block_reason.value if decision.block_reason else None,
        )
        return decision, state

    def _fk_session_id(self, session_id: uuid.UUID) -> uuid.UUID | None:
        """Session id for FK-bearing writes (audit_logs.session_id, wallet debit) — ADR-056 §4.

        For a temporary chat the session is synthetic and never persisted, so any write carrying an
        FK to ``chat_sessions`` MUST use NULL (the FK would otherwise fail). ``audit.record`` and
        ``wallet.consume`` both accept ``session_id=None`` (``_validate_session`` is skipped). For a
        normal chat this is the real, persisted session id (identity — no behavior change).
        """
        return None if self._temporary else session_id

    def _blocked(self, session_id: uuid.UUID, reason: BlockReason | None) -> ChatRunOut:
        resolved = reason or BlockReason.policy_denied
        blocked_requests_total.labels(reason=resolved.value).inc()
        return ChatRunOut(status="blocked", session_id=session_id, block_reason=resolved.value)

    async def _resolve_api_key(
        self, user_id: uuid.UUID, mode: Mode
    ) -> tuple[str | None, str | None]:
        """Resolve (plaintext api_key, byok_provider) for this turn (ADR-044 §5).

        - credits → ``(None, None)``: the service key of the active provider is used by the injected
          client; no provider routing.
        - byok → ``(plaintext_key, provider)``: the key is decrypted in-memory (never logged) and
          the provider is read from ``byok_keys.provider`` (fallback: detected from the plaintext
          for a legacy NULL row, ADR-044 §4). The provider routes generation to ``llm_client_for``
          in ``_generate_loop``. ``provider`` may be ``None`` only for a legacy key of unrecognized
          format → a defensive ``byok_invalid`` block downstream (unreachable for a valid key).
        """
        if mode is Mode.byok:
            byok_usage_share.set(1)
            resolved = await self._deps.byok.get_plaintext_key_with_provider(user_id)
            if resolved is None:
                # Policy should have blocked this; defensive.
                raise ValidationFailedError("byok key unavailable")
            return resolved
        byok_usage_share.set(0)
        return None, None  # service key used by the active provider's client

    async def _will_create_session(self, user_id: uuid.UUID, session_id: uuid.UUID | None) -> bool:
        """True when ``get_or_create_session`` would CREATE a new session (ADR-034 §3 model gate).

        Mirrors the repository's resume rule: a missing ``session_id``, or an absent / expired owned
        session, results in a create; an owned, non-expired session is a resume. Used only to gate
        the model-allowlist validation so the request ``model`` is ignored on resume (and validated
        before any new row is written on create). Read-only; the repository stays the single writer.
        """
        if session_id is None:
            return True
        existing = await self._deps.repo.get_session(session_id, user_id)
        if existing is None:
            return True
        return self._deps.repo.is_expired(existing)

    async def _build_messages(self, session_id: uuid.UUID) -> list[NeutralMessage]:
        """Reconstruct the provider-NEUTRAL history from chat_steps (TD-002, ADR-033 §3).

        Returns neutral messages; the active client translates them to provider wire messages
        (Anthropic ``tool_result`` block / OpenAI ``role=tool``). user/assistant carry the wire
        content blocks of the active provider from ``payload``; a tool step carries the domain
        tool-result record (incl. the raw ``providerToolUseId`` — ADR-008/BUG-4 — used to align
        tool_use ↔ tool_result on replay, never a domain UUID).
        """
        steps = await self._deps.repo.list_steps(session_id)
        messages: list[NeutralMessage] = []
        for step in steps:
            payload = step.payload
            if step.role == "user":
                messages.append(NeutralMessage(role="user", content_blocks=payload["content"]))
            elif step.role == "assistant":
                messages.append(NeutralMessage(role="assistant", content_blocks=payload["content"]))
            elif step.role == "tool":
                messages.append(
                    NeutralMessage(
                        role="tool",
                        tool_call_id=payload.get("toolCallId"),
                        provider_tool_use_id=payload["providerToolUseId"],
                        tool_name=payload.get("toolName"),
                        result=payload.get("result"),
                        error=payload.get("error"),
                    )
                )
        return messages

    async def _generate_loop(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        mode: Mode,
        billing: _BillingPlan,
        api_key: str | None,
        system_prompt: str,
        has_project: bool,
        byok_provider: str | None = None,
        first_turn_attachments: PreparedAttachments | None = None,
        model: str | None = None,
        dialog_mode: str | None = None,
        temporary: bool = False,
    ) -> ChatRunOut:
        # ADR-044 §5: select the generation client + the effective model by mode.
        # - credits → the injected active-provider client (self._deps.llm); stale-model guard
        #   against the ACTIVE provider allowlist (a session model fixed for another provider after
        #   an LLM_PROVIDER switch → None → client default, not a 502).
        # - byok → the client of the KEY's provider (llm_client_for), independent of LLM_PROVIDER;
        #   the session model is forwarded only if it is in the KEY provider's allowlist, else the
        #   BYOK default of that provider (a foreign model is never sent to the key's client).
        if mode is Mode.byok:
            if byok_provider is None:
                # Defensive (ADR-044 §5.1): a valid/enabled key always has a detectable provider.
                # An unrecognized legacy key reached generation → block, do not call any provider.
                await self._session.commit()
                return self._blocked(session_id, BlockReason.byok_invalid)
            llm = llm_client_for(byok_provider)
            effective_model = _model_for_provider(model, byok_provider)
            if effective_model is None:
                # §5.3: foreign/absent session model → the BYOK default of the key's provider.
                effective_model = get_settings().byok_default_model_for(byok_provider)
        else:
            llm = self._deps.llm
            # §Stale-model: guard the session model against the active provider's allowlist.
            effective_model = _model_for_provider(model, _active_provider())
        # ADR-055 §5 / ADR-059 §4: deep_thinking FORCES the instance reasoning model, overriding
        # BOTH the user's session model AND the stale-model guard above (incl. BYOK — the user's key
        # is spent on DEEP_THINKING_MODEL, accepted explicitly in ADR-059 §4). Other modes keep the
        # resolved effective_model. The provider-gate guaranteed OpenAI at session creation.
        if dialog_mode == "deep_thinking":
            effective_model = get_settings().deep_thinking_model
        # ADR-055 §5: forward the dialog mode to the client so it branches the Responses request
        # (deep_thinking → reasoning + encrypted replay; search → web_search tool; study_learn →
        # prompt-only this sprint). Anthropic ignores options (smart-only, ADR-059 §5).
        # ADR-056: `temporary` → the OpenAI Responses client sets store=False (the provider does not
        # persist the request); ignored by Anthropic (smart-only, ADR-059 §5).
        options = GenerationOptions(dialog_mode=cast(Any, dialog_mode), temporary=temporary)
        # ADR-011: server-side site.* tools are executed by the backend synchronously inside this
        # loop, WITHOUT a round-trip to iOS. We keep calling the LLM as long as the turn contains
        # ONLY server-side tools (their tool_results are produced here and fed straight back).
        # A turn with any client-side tool returns status=tool_call to iOS as before. A pure
        # assistant turn is the final step. The loop is bounded by MAX_SERVER_TOOL_ROUNDS (§2).
        max_rounds = get_settings().max_server_tool_rounds
        # ADR-028 Решение 2: accumulate the server-side tools executed across ALL rounds of THIS
        # call (one /chat/run or one /chat/tool-result continuation), in execution order. Threaded
        # into every terminal ChatRunOut of this loop so the client sees what ran, regardless of how
        # the turn ended (assistant_message / client tool_call / max_tokens).
        server_tools: list[ServerToolExecutionOut] = []
        # ADR-057: accumulate quizzes produced by quiz.generate across all rounds of THIS call. The
        # LAST element is surfaced as ChatRunOut.quiz ("последний квиз хода"). Threaded like
        # server_tools into every terminal ChatRunOut so the client sees the quiz regardless of how
        # the turn ended (normally the quiz precedes the final assistant_message).
        quiz_acc: list[dict[str, Any]] = []
        # ADR-058: accumulate images produced by image.generate across all rounds of THIS call
        # (append-all — a turn may generate several). Threaded like server_tools/quiz into every
        # terminal ChatRunOut so the client sees them regardless of how the turn ended.
        images_acc: list[GeneratedImageOut] = []
        # ADR-020 / ADR-033 §3: the PreparedAttachments are handed to the client on the FIRST
        # iteration ONLY; the client builds the provider content blocks and injects them into the
        # last user turn. Subsequent (tool-loop) iterations replay placeholders from chat_steps —
        # heavy base64 is never re-sent. The reference is consumed after the first call.
        turn0_attachments = first_turn_attachments
        for _ in range(max_rounds + 1):
            messages = await self._build_messages(session_id)
            # MAJOR-4: commit the persisted steps + audit BEFORE the network call so the pooled DB
            # connection is not held open for the whole LLM generation. Each subsequent
            # server-side round commits its own persisted tool_use/tool_result before re-calling.
            await self._session.commit()
            try:
                result: LLMResult = await llm.create_message(
                    system_prompt=system_prompt,
                    messages=messages,
                    # ADR-022 axis A: in «чистый чат» (no project) site.* (SERVER_SIDE_TOOLS) are
                    # NOT offered. Axis B (assistant_mode, Q-012-1) is not yet implemented; the
                    # effective set = this project gate over current behavior. Neutral tool defs;
                    # the client serializes them per provider (ADR-033 §4).
                    # ADR-056: temporary chat drops client-side tools (they need DB continuation via
                    # /chat/tool-result, unavailable without persistence); server-side (site.* /
                    # time.now) stay offered — executed in-request (ADR-011).
                    tools=neutral_tool_definitions(
                        include_server_side=has_project,
                        include_client_side=not temporary,
                        # ADR-057 §4: offer quiz.generate only in study_learn (session-fixed dialog
                        # mode, provider-gated to OpenAI at session creation).
                        dialog_mode=dialog_mode,
                    ),
                    attachments=turn0_attachments,
                    api_key=api_key,
                    # ADR-034 §4 / ADR-044 §5: the effective model resolved above (stale-model
                    # guard for credits; key-provider allowlist + BYOK default for byok). None → the
                    # client uses its provider default; the orchestrator never blindly forwards a
                    # foreign model.
                    model=effective_model,
                    # ADR-055 §5: dialog-mode generation options (deep_thinking / study_learn /
                    # search). Ignored by Anthropic (smart-only). `temporary` is sprint 3.
                    options=options,
                )
            except (AnthropicAuthError, OpenAIAuthError):
                if mode is Mode.byok:
                    # ADR-016: a previously-valid BYOK key rejected with 401 on use → expired
                    # (revoked/expired), not freshly invalid. Both map to byok_invalid in policy.
                    await self._deps.byok.mark_expired(user_id)
                    await self._session.commit()
                    return self._blocked(session_id, BlockReason.byok_invalid)
                raise
            # Consume the attachment override after the first call (placeholders only afterwards).
            turn0_attachments = None

            usage = result.usage.to_dict()
            token_usage_total.labels(direction="input", model=result.usage.model).inc(
                result.usage.input_tokens
            )
            token_usage_total.labels(direction="output", model=result.usage.model).inc(
                result.usage.output_tokens
            )

            # ADR-025: dispatch by stop_reason, NOT by the mere presence of tool_use blocks. A
            # max_tokens-truncated turn may carry incomplete tool_use blocks in content — they are
            # not executable and must NOT be surfaced; only the canonical tool_use stop reason
            # enters the tool branch. ADR-033 §2: compare against canonical (provider-neutral)
            # values; the client already mapped its wire stop_reason to these constants.
            if result.stop_reason == STOP_REASON_MAX_TOKENS:
                api_key = None
                # ADR-028: blocked+max_tokens may carry NON-empty server_tools (server-side rounds
                # could have run before the final turn was truncated).
                return await self._handle_max_tokens(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    result=result,
                    usage=usage,
                    server_tools=server_tools,
                    quiz_acc=quiz_acc,
                    images_acc=images_acc,
                )

            if result.stop_reason == STOP_REASON_TOOL_USE and result.tool_uses:
                outcome = await self._handle_tool_use(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    result=result,
                    usage=usage,
                    has_project=has_project,
                    server_tools=server_tools,
                    quiz_acc=quiz_acc,
                    images_acc=images_acc,
                    temporary=temporary,
                )
                # ADR-058 §5: an image.generate debit hit insufficient credits → the handler rolled
                # the turn back (bytes not saved); return the block WITHOUT committing.
                if outcome.blocked_out is not None:
                    api_key = None
                    return outcome.blocked_out
                # Persist the tool_use step + tool_calls + tool_results + audit (no billing here).
                await self._session.commit()
                if outcome.client_out is not None:
                    # A client-side tool is pending → hand off to iOS (drop the plaintext key).
                    # server_tools carries any server-side tools executed in this same turn BEFORE
                    # the client-side hand-off (ADR-028).
                    api_key = None
                    return outcome.client_out
                # Pure server-side turn: results are persisted; continue the loop to Anthropic.
                continue

            # Final assistant_message — break out of the server-side loop and bill once.
            api_key = None
            return await self._finalize_assistant(
                user_id=user_id,
                session_id=session_id,
                message_step_id=message_step_id,
                billing=billing,
                result=result,
                usage=usage,
                server_tools=server_tools,
                quiz_acc=quiz_acc,
                images_acc=images_acc,
            )

        # Exceeded MAX_SERVER_TOOL_ROUNDS consecutive server-side rounds (ADR-011 §2): controlled
        # failure + audit, never an infinite loop. No billing (no final assistant_message).
        api_key = None
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=self._fk_session_id(session_id),
                event_type=EVENT_CHAT_STEP,
                payload={
                    "sessionId": str(session_id),
                    "error": "max_server_tool_rounds_exceeded",
                    "maxRounds": max_rounds,
                },
            )
        )
        await self._session.commit()
        raise UpstreamError("server-side tool loop exceeded maximum rounds")

    async def _external_project_id(self, session_id: uuid.UUID) -> str:
        """external_project_id for site.* tools — from chat_sessions.project_id (session context).

        Never from model-supplied tool args (IDOR guard, website-builder/05-security.md).
        ADR-022 defensive-guard: called ONLY for sessions with a project (`project_id IS NOT NULL`);
        a NULL here is an upstream anomaly (site.* should not have been offered/executed).
        """
        sess = await self._session.get(ChatSession, session_id)
        if sess is None:  # pragma: no cover - session was just created/validated upstream
            raise NotFoundError("session not found")
        if sess.project_id is None:  # pragma: no cover - guarded by has_project before this call
            raise UpstreamError("site.* resolution attempted for a project-less session")
        return sess.project_id

    async def _finalize_assistant(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        billing: _BillingPlan,
        result: LLMResult,
        usage: dict[str, Any],
        server_tools: list[ServerToolExecutionOut],
        quiz_acc: list[dict[str, Any]],
        images_acc: list[GeneratedImageOut],
    ) -> ChatRunOut:
        # Final assistant_message. The assistant-step + billing (debit or trial flip) + audit are
        # committed together as one short transaction (atomicity per MAJOR-4 / CRITICAL-1).
        # ADR-023: capture the persisted assistant step's id → ChatResponse.stepId. It is the same
        # ChatStep.id that GET /v1/chats/{id} renders as ChatStepSchema.id for this step (sync
        # invariant).
        assistant_step = await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="assistant",
            payload={"content": result.content_blocks},
            usage=usage,
        )
        sess = await self._session.get(ChatSession, session_id)
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=self._fk_session_id(session_id),
                event_type=EVENT_CHAT_STEP,
                payload={
                    "sessionId": str(session_id),
                    "role": "assistant",
                    "model": usage.get("model"),
                    "usage": usage,
                },
            )
        )

        # CO-7 / ADR-002 / ADR-005: bill exactly once on the final assistant_message.
        # - active subscription + credits → consume 1 credit;
        # - trial (subscription=none, trial_used=false) → free, flip users.trial_used;
        # - byok / already-trial-used → free, no write.
        if billing.debit_credits:
            try:
                await self._debit(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    usage=usage,
                )
            except InsufficientCreditsError:
                # Balance dropped below 1 after policy allow → business block, not a tech error.
                # Roll back the assistant-step+audit so the unbillable step is not persisted.
                await self._session.rollback()
                return self._blocked(session_id, BlockReason.credits_empty)
        elif billing.mark_trial:
            # CRITICAL-1: consume the single lifetime trial atomically (idempotent).
            await self._deps.repo.mark_trial_used(user_id)

        if sess is not None:
            await self._deps.repo.touch_session(sess)

        await self._session.commit()
        return ChatRunOut(
            status="assistant_message",
            session_id=session_id,
            assistant_message=result.text,
            usage=usage,
            message_step_id=message_step_id,
            step_id=assistant_step.id,
            # ADR-028: server-side tools executed in this /chat/run before the final assistant turn.
            server_tools=list(server_tools),
            # ADR-057: the quiz (last of the turn) produced by quiz.generate before this final text.
            quiz=quiz_acc[-1] if quiz_acc else None,
            # ADR-058: all images generated in this turn (append-all).
            images=list(images_acc) if images_acc else None,
        )

    async def _handle_max_tokens(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        result: LLMResult,
        usage: dict[str, Any],
        server_tools: list[ServerToolExecutionOut],
        quiz_acc: list[dict[str, Any]],
        images_acc: list[GeneratedImageOut],
    ) -> ChatRunOut:
        """Handle a max_tokens-truncated turn (ADR-025 A2): blocked(max_tokens), NO debit.

        The turn was truncated by the output-token limit (stop_reason="max_tokens"). Its tool_use
        blocks (if any) are INCOMPLETE and must NOT be surfaced — toolCall(s) are omitted. The
        truncated assistant step IS persisted (history/diagnostics), but its incomplete tool_use
        blocks are excluded from continuation replay (re-entry by this turn is not supported). The
        response is status=blocked, blockReason=max_tokens with usage + message_step_id + step_id
        (unlike policy-blocked where they are null), assistantMessage = partial text if any. No
        credit is debited, no trial flip — the user does not pay for a truncated generation.
        """
        # Persist the truncated assistant step (for history/diagnostics). Its content is replayed
        # via _build_messages only as the assistant turn; since no tool_result will ever be sent
        # for its incomplete tool_use blocks, re-entry by this turn is not initiated (no pending
        # client tool_calls are created here — we do NOT call _handle_tool_use).
        truncated_step = await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="assistant",
            payload={"content": result.content_blocks},
            usage=usage,
        )
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=self._fk_session_id(session_id),
                event_type=EVENT_CHAT_STEP,
                payload={
                    "sessionId": str(session_id),
                    "role": "assistant",
                    "blockReason": BlockReason.max_tokens.value,
                    "model": usage.get("model"),
                    "usage": usage,
                },
            )
        )
        await self._session.commit()
        blocked_requests_total.labels(reason=BlockReason.max_tokens.value).inc()
        return ChatRunOut(
            status="blocked",
            session_id=session_id,
            # Partial text of the truncated turn (if Claude produced any) — clients may show
            # "ответ оборван". None when there was no text block.
            assistant_message=result.text or None,
            block_reason=BlockReason.max_tokens.value,
            usage=usage,
            message_step_id=message_step_id,
            step_id=truncated_step.id,
            # ADR-028: server-side rounds may have run before the final turn hit max_tokens →
            # surface them (this blocked row may be NON-empty, unlike policy-block).
            server_tools=list(server_tools),
            # ADR-057: a quiz.generate round may have completed before truncation → surface it.
            quiz=quiz_acc[-1] if quiz_acc else None,
            # ADR-058: images generated (and committed) in earlier rounds before truncation.
            images=list(images_acc) if images_acc else None,
        )

    async def _handle_tool_use(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        result: LLMResult,
        usage: dict[str, Any],
        has_project: bool,
        server_tools: list[ServerToolExecutionOut],
        quiz_acc: list[dict[str, Any]],
        images_acc: list[GeneratedImageOut],
        temporary: bool = False,
    ) -> _TurnOutcome:
        """Process a tool_use turn (ADR-008/011): persist tool_calls, branch server/client-side.

        For every tool_use block a tool_call row is persisted with its own domain id (uuid4) and
        raw provider_tool_use_id (toolu_..., never derived from the anthropic id — BUG-4). Then:
        - server-side (site.*): executed on the backend NOW; tool_call goes straight to status
          completed with the backend result; a tool step records the tool_result (replayed to
          Anthropic on continuation, ADR-011 §4). No round-trip to iOS.
        - client-side (files.*/...): left pending; ALL of them are returned as status=tool_call to
          iOS in toolCalls[] (ADR-025 parallel tool use); tool_call (singular, deprecated) =
          toolCalls[0]. The Anthropic tool-loop requires a tool_result for EVERY tool_use of the
          turn — surfacing only the first would orphan the rest → Anthropic 400 → 502.
        If the turn contains any client-side tool, client_out is set (hand off to iOS). If the turn
        is purely server-side, client_out is None and the orchestrator continues the loop.
        """
        # ADR-058 / MAJOR-4: generate image bytes BEFORE any DB write. add_step / create_tool_call /
        # audit each flush → a pooled connection is checked out and the turn transaction is open
        # until the loop commits. The (possibly multi-second) external images.generate call must NOT
        # run while a connection is held — concurrent generations would exhaust the pool and stall
        # ALL requests, not just generation. Here NO transaction is open (the loop committed before
        # create_message), so pre-generation holds no connection. Bytes/degrade are keyed by
        # provider_tool_use_id and consumed in the block loop below (debit + INSERT stay fast).
        image_pregen = await self._pregenerate_images(result.tool_uses)

        # Persist the assistant tool_use step (no debit on tool-rounds). ADR-023: this is the
        # step-of-record for a status=tool_call response — ChatResponse.stepId = its ChatStep.id
        # (the history step whose payload carries the tool_use block). NOT toolCall.id.
        assistant_step = await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="assistant",
            payload={"content": result.content_blocks},
            usage=usage,
        )

        # ADR-022 §2/§4 defensive-guard: _external_project_id() (which resolves the project for
        # site.* execution) is resolved ONLY when the session has a project. Without a project,
        # site.* were not offered to Claude, so this path is unreachable in normal operation; if
        # Claude returns a site.* tool_use anyway (upstream anomaly), we must NOT execute it and
        # must NOT resolve a project — see the per-block guard below.
        external_project_id = await self._external_project_id(session_id) if has_project else None
        # ADR-025: collect ALL client-side tool calls of this turn (in block order) → toolCalls[].
        client_outs: list[ToolCallOut] = []
        for block in result.tool_uses:
            tool_name = str(block["name"])
            provider_tool_use_id = str(block["id"])  # raw anthropic "toolu_...", opaque

            # ADR-022 defensive-guard: a server-side site.* tool_use with no project must never be
            # executed (the tool was not offered; this is an upstream anomaly, treated like an
            # unknown tool name — ADR-008). Fail before validating args / resolving any project.
            if tool_name in SERVER_SIDE_TOOLS and not has_project:
                raise UpstreamError("server-side site.* tool requested for a project-less session")

            # Validate the model-produced args against the strict schema. For quiz.generate
            # (ADR-057 §3) invalid args DEGRADE to a tool_result error the model fixes in-loop —
            # exactly like time.now's invalid_timezone (ADR-026 §6) — NOT a 422 that drops the turn:
            # the cleaned OpenAI-strict schema cannot express the quiz invariants (2..10 options,
            # length caps, correctIndex range), so a violation by the model is an EXPECTED case, not
            # an anomaly. On failure the RAW args are stored/forwarded and the handler
            # (_quiz_generate) re-validates → content-free tool_result error (TD-035). Every OTHER
            # tool keeps the hard 422 on invalid args (unchanged).
            try:
                validated_args = validate_tool_args(tool_name, dict(block["input"]))
            except ValueError as exc:
                if tool_name != TOOL_QUIZ_GENERATE:
                    raise ValidationFailedError(str(exc)) from exc
                # quiz.generate degrade path: forward the raw args; _quiz_generate re-validates and
                # returns ToolExecution.error("invalid_quiz", ...) → tool_result the model can fix.
                validated_args = dict(block["input"])

            tool_call_id = uuid.uuid4()  # domain id: fresh UUID, independent of anthropic id
            await self._deps.repo.create_tool_call(
                session_id=session_id,
                message_step_id=message_step_id,
                tool_name=tool_name,
                args=validated_args,
                tool_call_id=tool_call_id,
                provider_tool_use_id=provider_tool_use_id,
            )
            await self._deps.audit.record(
                AuditEvent(
                    user_id=user_id,
                    session_id=self._fk_session_id(session_id),
                    event_type=EVENT_TOOL_CALL_INITIATED,
                    payload={"toolCallId": str(tool_call_id), "toolName": tool_name},
                )
            )

            if tool_name == TOOL_IMAGE_GENERATE:
                # ADR-058: dedicated path — bytes were PRE-generated (above, no connection held);
                # here only the fast DB work runs: debit IMAGE_CREDITS_COST → INSERT. A
                # content-policy/other failure degrades to a tool_result error (turn survives);
                # insufficient credits blocks the whole turn (bytes not saved, rollback).
                blocked = await self._execute_image_generate_tool(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    tool_call_id=tool_call_id,
                    args=validated_args,
                    provider_tool_use_id=provider_tool_use_id,
                    pregen=image_pregen[provider_tool_use_id],
                    temporary=temporary,
                    server_tools=server_tools,
                    images_acc=images_acc,
                )
                if blocked is not None:
                    # ADR-058 §5: image_credits_empty — the turn is rolled back and blocked. Stop
                    # processing further blocks; signal the loop to return without committing.
                    return _TurnOutcome(client_out=None, blocked_out=blocked)
            elif tool_name in GLOBAL_SERVER_SIDE_TOOLS:
                # ADR-026 §4: global server-side (time.now) is routed BEFORE the project-scoped
                # branch — executed immediately WITHOUT external_project_id and WITHOUT the
                # has_project guard. «Нет проекта» is the normal mode here, not an anomaly.
                await self._execute_global_server_side_tool(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    args=validated_args,
                    provider_tool_use_id=provider_tool_use_id,
                    server_tools=server_tools,
                    quiz_acc=quiz_acc,
                )
            elif tool_name in SERVER_SIDE_TOOLS:
                # Invariant (ADR-022): reaching here implies has_project is True (the project-less
                # site.* anomaly raised above), so external_project_id is a resolved string. The
                # assert applies ONLY to project-scoped site.* (ADR-026 §4).
                assert external_project_id is not None  # noqa: S101 - ADR-022 guard invariant
                await self._execute_server_side_tool(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    args=validated_args,
                    provider_tool_use_id=provider_tool_use_id,
                    external_project_id=external_project_id,
                    server_tools=server_tools,
                )
            elif temporary:
                # ADR-056: client-side tools were NOT offered in a temporary chat (they need DB
                # continuation). A client-side tool_use here is an upstream anomaly — treat it as
                # UpstreamError, symmetric to the project-less site.* guard above. Never hand off to
                # iOS (continuation via /chat/tool-result is impossible without persistence).
                raise UpstreamError("client-side tool requested in a temporary chat")
            else:
                # Client-side: leave pending; surface in toolCalls[] (ADR-025).
                client_outs.append(
                    ToolCallOut(id=str(tool_call_id), name=tool_name, args=validated_args)
                )

        if client_outs:
            return _TurnOutcome(
                client_out=ChatRunOut(
                    status="tool_call",
                    session_id=session_id,
                    # ADR-024 §3 / Q-024-1 (variant A): carry the accompanying text of THIS same
                    # assistant step (the one whose tool_use blocks are returned). result.text is
                    # the concatenation of this turn's text blocks; empty → None (no text).
                    assistant_message=result.text or None,
                    # ADR-025: ALL client-side calls; tool_call (deprecated) = toolCalls[0].
                    tool_calls=client_outs,
                    tool_call=client_outs[0],
                    usage=usage,
                    message_step_id=message_step_id,
                    step_id=assistant_step.id,
                    # ADR-028: any server-side tools executed in this turn BEFORE the client-side
                    # hand-off are surfaced (snapshot — copy, not the live accumulator).
                    server_tools=list(server_tools),
                    # ADR-057: a quiz.generate in this same turn (before the client-side tool) is
                    # surfaced on the tool_call response too (last quiz of the turn).
                    quiz=quiz_acc[-1] if quiz_acc else None,
                    # ADR-058: images generated in this same turn (before the client-side tool).
                    images=list(images_acc) if images_acc else None,
                )
            )
        # Purely server-side turn → continue the loop (no hand-off to iOS).
        return _TurnOutcome(client_out=None)

    async def _execute_server_side_tool(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        tool_name: str,
        args: dict[str, Any],
        provider_tool_use_id: str,
        external_project_id: str,
        server_tools: list[ServerToolExecutionOut],
    ) -> None:
        """Execute a site.* tool on the backend and persist its tool_result (ADR-011 §1, §4).

        The tool_call is moved to status=completed immediately (no client tool_result is awaited).
        The tool step stores the providerToolUseId so _build_messages replays the continuation with
        a consistent id pair (ADR-008). MUTATING audit (site.write_file/site.delete → tool_mutation)
        is recorded inside the handler, in this same transaction (audit/03-architecture).
        ADR-028: append a COMPACT (status + summary, NO raw result/path/URL/token) entry to
        server_tools for the /chat/run response.
        """
        execution = await self._deps.site_tools.execute(
            tool_name=tool_name,
            args=args,
            user_id=user_id,
            external_project_id=external_project_id,
            session_id=session_id,
        )
        payload = execution.to_tool_result_payload()
        status = "errored" if execution.is_error else "completed"
        # ADR-028 Решение 2: record the server-side execution (domain name, status, summary).
        # _server_tool_summary deliberately ignores the raw payload — only "ok" / short error code.
        server_tools.append(
            ServerToolExecutionOut(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=status,
                summary=_server_tool_summary(execution),
            )
        )
        await self._deps.repo.complete_tool_call(
            tool_call_id=tool_call_id,
            status=status,
            result=payload,
        )
        await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="tool",
            payload={
                "toolCallId": str(tool_call_id),
                "providerToolUseId": provider_tool_use_id,
                "toolName": tool_name,
                "result": payload.get("result"),
                "error": payload.get("error"),
            },
        )
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=self._fk_session_id(session_id),
                event_type=EVENT_TOOL_CALL_COMPLETED,
                payload={
                    "toolCallId": str(tool_call_id),
                    "toolName": tool_name,
                    "status": status,
                },
            )
        )

    async def _execute_global_server_side_tool(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        tool_name: str,
        args: dict[str, Any],
        provider_tool_use_id: str,
        server_tools: list[ServerToolExecutionOut],
        quiz_acc: list[dict[str, Any]],
    ) -> None:
        """Execute a global server-side tool (time.now / quiz.generate) on the backend (ADR-026 §4,
        §6; ADR-057 §4).

        Mirrors _execute_server_side_tool but is PROJECT-INDEPENDENT: no external_project_id is
        resolved or passed (these tools are global). The tool_call is moved to status=completed
        immediately (no client tool_result is awaited); the tool step stores providerToolUseId so
        _build_messages replays the continuation with a consistent id pair (ADR-008). Neither
        time.now nor quiz.generate is in MUTATING_TOOLS → no tool_mutation audit; only the standard
        tool_call_completed audit is recorded. Billing is unchanged (server-side round adds no
        debit, ADR-006). ADR-028: append a COMPACT (status + summary, NO raw result) entry.
        ADR-057: for a successful quiz.generate, lift the echoed quiz dict into quiz_acc → the
        response's ChatRunOut.quiz (the summary/audit stay content-free — TD-035).
        """
        execution = await self._deps.global_tools.execute(tool_name=tool_name, args=args)
        payload = execution.to_tool_result_payload()
        status = "errored" if execution.is_error else "completed"
        # ADR-057: capture the quiz payload (last-wins) for ChatRunOut.quiz. Only on a successful
        # quiz.generate; an errored execution (cannot happen — args pre-validated) is skipped.
        if (
            tool_name == TOOL_QUIZ_GENERATE
            and not execution.is_error
            and execution.result is not None
        ):
            quiz_acc.append(execution.result)
        # ADR-028 Решение 2: record the execution (domain name, status, compact summary — for
        # quiz.generate the summary stays "ok", the learning content never leaks here, TD-035).
        server_tools.append(
            ServerToolExecutionOut(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=status,
                summary=_server_tool_summary(execution),
            )
        )
        await self._deps.repo.complete_tool_call(
            tool_call_id=tool_call_id,
            status=status,
            result=payload,
        )
        await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="tool",
            payload={
                "toolCallId": str(tool_call_id),
                "providerToolUseId": provider_tool_use_id,
                "toolName": tool_name,
                "result": payload.get("result"),
                "error": payload.get("error"),
            },
        )
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=self._fk_session_id(session_id),
                event_type=EVENT_TOOL_CALL_COMPLETED,
                payload={
                    "toolCallId": str(tool_call_id),
                    "toolName": tool_name,
                    "status": status,
                },
            )
        )

    async def _pregenerate_images(self, tool_uses: list[dict[str, Any]]) -> dict[str, _ImagePregen]:
        """Generate bytes for EVERY image.generate block BEFORE any DB write (ADR-058, MAJOR-4).

        Called at the top of _handle_tool_use, before add_step opens the turn transaction — so NO
        pooled connection is held during the (possibly multi-second) external images.generate
        call(s). Keyed by provider_tool_use_id (unique per turn). Invalid image args →
        ValidationFailedError (422) here, BEFORE any persistence (same hard-422 as any non-quiz tool
        in the block loop, just earlier — nothing to roll back). A generation exception is CAPTURED
        (not raised): the base ``ImageGenerationError`` is caught (it covers the
        ``ImageContentPolicyError`` subclass) and stored; classification + logging happen in
        _execute_image_generate_tool where the domain tool_call_id exists (TD-035 log needs it). No
        session/DB access here — this method holds no connection.
        """
        pregen: dict[str, _ImagePregen] = {}
        for block in tool_uses:
            if str(block["name"]) != TOOL_IMAGE_GENERATE:
                continue
            provider_tool_use_id = str(block["id"])
            try:
                validated = validate_tool_args(TOOL_IMAGE_GENERATE, dict(block["input"]))
            except ValueError as exc:
                # Invalid image args → hard 422 (like any non-quiz tool), before any DB write.
                raise ValidationFailedError(str(exc)) from exc
            try:
                data = await self._deps.global_tools.generate_image(
                    prompt=str(validated["prompt"]),
                    size=validated.get("size"),
                    quality=validated.get("quality"),
                )
            except ImageGenerationError as exc:
                # Capture BOTH classes (ImageContentPolicyError ⊂ ImageGenerationError); the correct
                # ordering (policy first) is applied by isinstance in _execute_image_generate_tool.
                pregen[provider_tool_use_id] = _ImagePregen(data=None, error=exc)
            else:
                pregen[provider_tool_use_id] = _ImagePregen(data=data, error=None)
        return pregen

    async def _execute_image_generate_tool(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        args: dict[str, Any],
        provider_tool_use_id: str,
        pregen: _ImagePregen,
        temporary: bool,
        server_tools: list[ServerToolExecutionOut],
        images_acc: list[GeneratedImageOut],
    ) -> ChatRunOut | None:
        """Persist an image.generate outcome: debit → INSERT (bytes were PRE-generated, MAJOR-4).

        The bytes are already in ``pregen`` (generated before any DB write, so no pooled connection
        was held during the slow external call). Only the FAST DB work runs here. Order (ADR-058
        §5): (1) replay a degrade if generation failed; else (2) DEBIT ``IMAGE_CREDITS_COST`` —
        ALWAYS, regardless of ``mode`` (the image is generated on our server key, so it costs us in
        every mode — ADR-058 §4 revised); (3) only on a successful debit INSERT the row. Returns:

        No ``billing`` param — the image debit is UNCONDITIONAL (charged in credits, byok AND trial,
        ADR-058 §4 revised). The per-turn TEXT credit is still gated by the turn ``billing`` plan in
        _finalize_assistant (byok/trial text stays free).
        - ``None`` on success OR a graceful degrade (content-policy / other generation failure →
          tool_result error, the turn survives, §3);
        - a ``blocked`` ChatRunOut when the debit hits insufficient credits — the whole turn is
          rolled back (bytes NOT saved) and ``blockReason=image_credits_empty`` (§5).

        Idempotency (ADR-025/§4): the debit key is the domain ``tool_call_id``; the partial-unique
        index ``ux_generated_images_tool_call`` blocks a duplicate row (defense-in-depth — the
        server-side tool-loop is not replayed on continuation, which returns the saved final step).
        TD-035: the prompt is NEVER logged nor placed in the audit payload / summary.
        """
        prompt = str(args["prompt"])

        # (1) A pre-generation failure DEGRADES to a tool_result error (turn survives, ADR-058 §3) —
        #     NOT a 502. Classify by isinstance in the correct order: ImageContentPolicyError (the
        #     subclass) FIRST, else the base ImageGenerationError. This mirrors the except-order
        #     invariant; the domain tool_call_id (absent at pre-gen time) is available here for the
        #     TD-035 log. No debit happens on a failed generation.
        if pregen.error is not None:
            exc = pregen.error
            if isinstance(exc, ImageContentPolicyError):
                # Content-policy refusal is a NORMAL user outcome, not an incident — quiet INFO; the
                # model adjusts the prompt in the same turn. TD-035: no prompt/key, only toolCallId.
                image_generation_errors_total.labels(result="content_policy").inc()
                log_event(
                    logger,
                    logging.INFO,
                    "image_content_policy_refused",
                    toolCallId=str(tool_call_id),
                    status="errored",
                )
                execution = ToolExecution.error(
                    "content_policy",
                    "the image prompt was rejected by the content policy; adjust the prompt "
                    "and try again",
                )
            else:
                # Any NON-policy failure (upstream/network/oversize/misconfigured key). Degrade
                # OUTWARD but emit a structured WARNING + metric so an infra fault stays observable
                # (symmetry TD-014: degrade out, log in). TD-035: only error CLASS + status +
                # toolCallId — never the prompt, key, or exception message.
                image_generation_errors_total.labels(result="generation_failed").inc()
                log_event(
                    logger,
                    logging.WARNING,
                    "image_generation_failed",
                    toolCallId=str(tool_call_id),
                    errorClass=type(exc).__name__,
                    status="errored",
                )
                execution = ToolExecution.error(
                    "image_generation_failed",
                    "image generation is temporarily unavailable; try again later",
                )
            await self._persist_image_tool_outcome(
                user_id=user_id,
                session_id=session_id,
                message_step_id=message_step_id,
                tool_call_id=tool_call_id,
                provider_tool_use_id=provider_tool_use_id,
                execution=execution,
                image_id=None,
                server_tools=server_tools,
            )
            return None

        assert pregen.data is not None  # noqa: S101 - error is None → data is present
        image_data = pregen.data

        # (2) Debit IMAGE_CREDITS_COST — ALWAYS, independent of `mode` (ADR-058 §4, revised). The
        #     image is ALWAYS generated on OUR server OPENAI_API_KEY (image_client.py), so it costs
        #     us in every mode. Unlike the per-turn text credit — which BYOK pays with the user's
        #     own key and so is free — the image debit applies to credits, byok AND trial:
        #       credits → 1 + N×COST | byok → 0 + N×COST | trial → text free, image needs a balance.
        #     A trial user with a zero balance who asks for an image is blocked here
        #     (image_credits_empty) WITHOUT consuming the trial (mark_trial runs after the tool-loop
        #     in _finalize_assistant; this rollback returns before that), so they can retry
        #     text-only.
        try:
            await self._deps.wallet.consume(
                user_id=user_id,
                amount=get_settings().image_credits_cost,
                idempotency_key=str(tool_call_id),
                meta={"kind": "image", "contentType": image_data.media_type},
                # ADR-056 §4: NULL session id for a temporary chat (no persisted session).
                session_id=self._fk_session_id(session_id),
            )
        except InsufficientCreditsError:
            # ADR-058 §5: bytes NOT saved. consume inserted-then-failed its ledger row; roll the
            # whole turn back (discards that row + the tool_use step of this round) and block.
            await self._session.rollback()
            return self._blocked(session_id, BlockReason.image_credits_empty)

        # (3) INSERT generated_images (only after a successful/free debit). expires_at is on the DB
        #     CLOCK (now() + make_interval) so created_at + TTL uses ONE clock — the same one that
        #     GET / sweep compare against (ADR-058 §6): a temporary chat expires after the TTL, a
        #     normal chat never expires (NULL).
        image_id = await self._insert_generated_image(
            user_id=user_id,
            session_id=self._fk_session_id(session_id),
            message_step_id=message_step_id,
            tool_call_id=tool_call_id,
            content=image_data.data,
            content_type=image_data.media_type,
            prompt=prompt,
            temporary=temporary,
            ttl_seconds=get_settings().temporary_image_ttl_seconds,
        )
        images_acc.append(
            GeneratedImageOut(
                image_id=image_id,
                content_type=image_data.media_type,
                size=len(image_data.data),
            )
        )
        # tool_result carries ONLY id/contentType/size — NEVER bytes/base64 (ADR-058 §1).
        await self._persist_image_tool_outcome(
            user_id=user_id,
            session_id=session_id,
            message_step_id=message_step_id,
            tool_call_id=tool_call_id,
            provider_tool_use_id=provider_tool_use_id,
            execution=ToolExecution.ok(
                {
                    "imageId": str(image_id),
                    "contentType": image_data.media_type,
                    "size": len(image_data.data),
                }
            ),
            image_id=image_id,
            server_tools=server_tools,
        )
        return None

    async def _insert_generated_image(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID | None,
        message_step_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        content: bytes,
        content_type: str,
        prompt: str,
        temporary: bool,
        ttl_seconds: int,
    ) -> uuid.UUID:
        """INSERT the image bytes into generated_images, idempotent by tool_call_id (ADR-058 §1).

        Uses ``ON CONFLICT DO NOTHING`` on the partial-unique index
        ``ux_generated_images_tool_call`` (``WHERE tool_call_id IS NOT NULL``); a conflict (a row
        already exists for this tool_call_id) yields no RETURNING row → the existing id is fetched
        instead. This is defense-in-depth: the server-side tool-loop is not replayed on
        continuation, so a conflict is not reached in normal operation. Uses the REAL session
        (``self._session``) even for a temporary chat — the image
        row IS persisted (with ``session_id=NULL`` + TTL), unlike the chat_* tables (ADR-056 §5).

        ``expires_at`` (ADR-058 §6) is computed on the DATABASE CLOCK: ``now() + make_interval(secs
        => ttl)`` for a temporary chat, ``NULL`` for a normal chat. Using the DB clock keeps the TTL
        formula ``created_at + TEMPORARY_IMAGE_TTL_SECONDS`` on ONE clock — the same ``now()`` the
        fetch query and the sweep compare against — so app↔DB clock skew cannot drift the TTL.
        """
        # DB-side interval so created_at (DB now()) and expires_at use the same clock (§6).
        expires_expr: Any = (
            func.now() + func.make_interval(0, 0, 0, 0, 0, 0, ttl_seconds) if temporary else None
        )
        stmt = (
            pg_insert(GeneratedImage)
            .values(
                user_id=user_id,
                session_id=session_id,
                message_step_id=message_step_id,
                tool_call_id=tool_call_id,
                content=content,
                content_type=content_type,
                size=len(content),
                prompt=prompt,
                expires_at=expires_expr,
            )
            .on_conflict_do_nothing(
                index_elements=["tool_call_id"],
                index_where=text("tool_call_id IS NOT NULL"),
            )
            .returning(GeneratedImage.id)
        )
        image_id = await self._session.scalar(stmt)
        if image_id is None:
            image_id = await self._session.scalar(
                select(GeneratedImage.id).where(GeneratedImage.tool_call_id == tool_call_id)
            )
        assert image_id is not None  # noqa: S101 - just inserted or already present
        return image_id

    async def _persist_image_tool_outcome(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        provider_tool_use_id: str,
        execution: ToolExecution,
        image_id: uuid.UUID | None,
        server_tools: list[ServerToolExecutionOut],
    ) -> None:
        """Persist an image.generate outcome: complete the tool_call, add the tool step, audit,
        and append the COMPACT serverTools entry (ADR-058 / ADR-028).

        Mirrors _execute_global_server_side_tool's persistence, but the tool_result carries only
        id/contentType/size (success) or a machine error code (degrade) — never bytes (ADR-058 §1).
        TD-035: the audit payload is toolCallId/toolName/status (+ imageId on success) ONLY — never
        the prompt; the summary stays content-free ("ok" / short error code).
        """
        payload = execution.to_tool_result_payload()
        status = "errored" if execution.is_error else "completed"
        server_tools.append(
            ServerToolExecutionOut(
                tool_call_id=tool_call_id,
                tool_name=TOOL_IMAGE_GENERATE,
                status=status,
                summary=_server_tool_summary(execution),
            )
        )
        await self._deps.repo.complete_tool_call(
            tool_call_id=tool_call_id,
            status=status,
            result=payload,
        )
        await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="tool",
            payload={
                "toolCallId": str(tool_call_id),
                "providerToolUseId": provider_tool_use_id,
                "toolName": TOOL_IMAGE_GENERATE,
                "result": payload.get("result"),
                "error": payload.get("error"),
            },
        )
        audit_payload: dict[str, Any] = {
            "toolCallId": str(tool_call_id),
            "toolName": TOOL_IMAGE_GENERATE,
            "status": status,
        }
        if image_id is not None:
            audit_payload["imageId"] = str(image_id)
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=self._fk_session_id(session_id),
                event_type=EVENT_TOOL_CALL_COMPLETED,
                payload=audit_payload,
            )
        )

    async def _debit(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        usage: dict[str, Any],
    ) -> None:
        # amount is fixed at 1 (1 credit = 1 message, ADR-006); idempotent by messageStepId.
        # InsufficientCreditsError propagates to the caller, which maps it to a credits_empty block.
        await self._deps.wallet.consume(
            user_id=user_id,
            amount=1,
            idempotency_key=str(message_step_id),
            meta={"usage": usage, "model": usage.get("model")},
            # ADR-056 §4: NULL session id for a temporary chat (no persisted session to FK to);
            # identity for a normal chat. wallet.consume skips _validate_session when None.
            session_id=self._fk_session_id(session_id),
        )

    def _render_saved_step(
        self,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        step: ChatStep | None,
    ) -> ChatRunOut:
        if step is None:
            # Nothing generated yet for this step (e.g. concurrent in-flight) → treat as not found.
            raise NotFoundError("no completed step for tool result")
        text = ""
        for block in step.payload.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        # ADR-023: idempotent replay returns the same sync ids as the original response — the turn
        # (message_step_id, stable across re-entry) and the saved step's own id.
        return ChatRunOut(
            status="assistant_message",
            session_id=session_id,
            assistant_message=text,
            usage=step.usage,
            message_step_id=message_step_id,
            step_id=step.id,
        )


def decision_allow(decision: Decision) -> bool:
    return decision.allow
