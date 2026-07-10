"""In-memory ChatRepository for the temporary chat (ADR-056).

A temporary chat persists NOTHING in ``chat_sessions`` / ``chat_steps`` / ``tool_calls``: the
client sends the history, the server keeps the turn only in memory. ``EphemeralChatRepository``
subclasses :class:`ChatRepository` so it satisfies the exact same signatures (mypy strict on
``app.chat.*``) and can be swapped into the orchestrator's ``_Deps.repo`` — the invariant «only
``ChatRepository`` writes the chat-* tables» (ADR-021) is preserved because the persisting
``ChatRepository`` never runs in this path.

Every chat-table read/write is overridden to operate on in-memory state (a step list + a tool_call
dict). ``mark_trial_used`` is NOT overridden: it writes the ``users`` table (the lifetime trial),
NOT a chat-* table, so it must hit the real DB — a temporary chat consumes the trial exactly like a
normal chat (ADR-056 §4: temporary ≠ free). The billing debit (``wallet.consume``) is likewise a
real write; the orchestrator passes ``session_id=None`` there so no FK to the (non-existent) session
is required.

Seeding (ADR-056 §1): the client transcript is provider-agnostic text. The seed content blocks are
built in the ACTIVE provider's replay shape so that the orchestrator's ``_build_messages`` →
``LLMClient`` replay is wire-valid on both providers. ONLY text user/assistant turns are seeded —
raw ``tool_use`` without a paired ``tool_result`` would make the provider reject the request (BUG-5,
ADR-021).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.repository import ChatRepository, SessionContext
from app.models import ChatSession, ChatStep, ToolCall
from app.schemas.chat import TemporaryTurn


def _history_blocks(role: str, content: str, provider: str) -> list[dict[str, Any]]:
    """Build the provider-correct replay content blocks for one transcript turn (ADR-056 §1).

    The orchestrator's ``_build_messages`` feeds these verbatim to the active client:
    - Anthropic replays user/assistant ``content`` as-is → a plain ``{type:text, text}`` block works
      for both roles.
    - OpenAI (Responses) replays a user turn via ``input_text`` (accepts ``{type:text}`` blocks)
      but an assistant turn is ``extend``-ed into the ``input`` list verbatim, so it must be a valid
      Responses input item — an ``EasyInputMessage`` ``{role:assistant, content}`` (a bare text
      block is not a valid top-level input item).
    """
    if role == "assistant" and provider == "openai":
        return [{"role": "assistant", "content": content}]
    return [{"type": "text", "text": content}]


class EphemeralChatRepository(ChatRepository):
    """In-memory ChatRepository for a temporary chat (ADR-056). Persists no chat-* rows."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        seed: list[TemporaryTurn],
        provider: str,
    ) -> None:
        # The real session is kept only so the INHERITED mark_trial_used (users-table write) works;
        # no chat-* row is ever added to it from here.
        super().__init__(session)
        # Synthetic session id, minted once and reused by get_or_create_session — never written to
        # the DB. Returned to the client as ChatRunOut.session_id.
        self._session_id = uuid.uuid4()
        self._tool_calls: dict[uuid.UUID, ToolCall] = {}
        self._steps: list[ChatStep] = [
            ChatStep(
                session_id=self._session_id,
                message_step_id=uuid.uuid4(),
                role=turn.role,
                payload={"content": _history_blocks(turn.role, turn.content, provider)},
            )
            for turn in seed
        ]

    async def get_session(self, session_id: uuid.UUID, user_id: uuid.UUID) -> ChatSession | None:
        # A temporary session is never persisted → not resolvable by id.
        return None

    async def get_or_create_session(
        self,
        *,
        user_id: uuid.UUID,
        project_id: str | None,
        mode: str,
        session_id: uuid.UUID | None,
        assistant_mode: str = "chat",
        dialog_mode: str = "smart",
        title: str | None = None,
        model: str | None = None,
        workspace_project_id: uuid.UUID | None = None,
    ) -> SessionContext:
        # Always a NEW synthetic session (temporary chats cannot resume — enforced at the schema
        # layer: temporary + sessionId → 422). The ChatSession is constructed but NEVER added to the
        # AsyncSession, so nothing reaches chat_sessions (ADR-056 §1, invariant ADR-021).
        session = ChatSession(
            id=self._session_id,
            user_id=user_id,
            project_id=project_id,
            mode=mode,
            assistant_mode=assistant_mode,
            dialog_mode=dialog_mode,
            title=title,
            model=model,
            workspace_project_id=workspace_project_id,
        )
        return SessionContext(session=session, is_new=True)

    async def touch_session(self, session: ChatSession) -> None:
        # No persisted row to touch.
        return None

    async def add_step(
        self,
        *,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        role: str,
        payload: dict[str, Any],
        usage: dict[str, Any] | None = None,
    ) -> ChatStep:
        # In-memory only: append so a later _build_messages sees it (the server-side tool-loop needs
        # the tool_use/tool_result steps replayed). The returned step's `id` stays None (never
        # flushed) → ChatRunOut.step_id = None for a temporary chat (ADR-056: stepId=null).
        step = ChatStep(
            session_id=session_id,
            message_step_id=message_step_id,
            role=role,
            payload=payload,
            usage=usage,
        )
        self._steps.append(step)
        return step

    async def list_steps(self, session_id: uuid.UUID) -> list[ChatStep]:
        # Seed transcript + steps added during this request, in insertion order.
        return list(self._steps)

    async def create_tool_call(
        self,
        *,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_name: str,
        args: dict[str, Any],
        tool_call_id: uuid.UUID,
        provider_tool_use_id: str,
    ) -> ToolCall:
        row = ToolCall(
            id=tool_call_id,
            session_id=session_id,
            message_step_id=message_step_id,
            tool_name=tool_name,
            provider_tool_use_id=provider_tool_use_id,
            args=args,
            status="pending",
        )
        self._tool_calls[tool_call_id] = row
        return row

    async def get_tool_call(self, tool_call_id: uuid.UUID) -> ToolCall | None:
        return self._tool_calls.get(tool_call_id)

    async def list_tool_calls_for_step(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> list[ToolCall]:
        return [
            tc
            for tc in self._tool_calls.values()
            if tc.session_id == session_id and tc.message_step_id == message_step_id
        ]

    async def complete_tool_call(
        self,
        *,
        tool_call_id: uuid.UUID,
        status: str,
        result: dict[str, Any] | None,
    ) -> bool:
        row = self._tool_calls.get(tool_call_id)
        if row is None or row.status in ("completed", "errored"):
            return False
        row.status = status
        row.result = result
        return True

    async def truncate_from_message_step(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> int | None:
        # Editing is not available for a temporary chat (temporary + editMessageStepId → 422).
        return None

    async def assistant_tool_step_id(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> uuid.UUID | None:
        # No persisted step ids for a temporary chat (client-side continuation is unavailable).
        return None

    async def next_step_after(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID, after_tool_call: uuid.UUID
    ) -> ChatStep | None:
        # Idempotent replay is a persisted-continuation concern; temporary chats have none.
        return None
