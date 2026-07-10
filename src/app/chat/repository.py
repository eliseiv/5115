"""Chat persistence: sessions, steps, tool_calls + context reconstruction (CO-3, chat/04).

Only this module writes chat_sessions / chat_steps / tool_calls. Context for Claude is
reconstructed from chat_steps on each step (TD-002). Soft TTL 24h by updated_at (Q-001-1):
continuing an expired session starts a new session.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import ChatSession, ChatStep, ToolCall

# Default max length of an auto-generated chat title (chats/03-architecture.md).
_TITLE_MAX_CHARS = 60


@dataclass(frozen=True)
class SessionContext:
    session: ChatSession
    is_new: bool


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def derive_title(message: str, limit: int = _TITLE_MAX_CHARS) -> str | None:
    """Auto-generate a chat title from the first user message (chats/03, BR-CH-2).

    Whitespace-normalized and truncated to ``limit`` chars. Returns None for an
    empty/whitespace-only message (the list then falls back to preview).
    """
    normalized = " ".join(message.split())
    if not normalized:
        return None
    return normalized[:limit]


class ChatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def mark_trial_used(self, user_id: uuid.UUID) -> bool:
        """Atomically consume the single lifetime trial (ADR-005, BR-1).

        UPDATE ... WHERE trial_used = FALSE → idempotent: returns True if this call flipped it,
        False if it was already used (concurrent retry / replay).
        """
        updated = await self._session.scalar(
            text(
                "UPDATE users SET trial_used = TRUE "
                "WHERE id = :uid AND trial_used = FALSE RETURNING id"
            ),
            {"uid": str(user_id)},
        )
        return updated is not None

    async def get_session(self, session_id: uuid.UUID, user_id: uuid.UUID) -> ChatSession | None:
        row = await self._session.scalar(
            select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user_id)
        )
        return row

    def is_expired(self, session: ChatSession) -> bool:
        """True when the session has exceeded the soft TTL (Q-001-1) → a new session on resume.

        Public so callers that need the same resume rule WITHOUT writing (e.g. the ADR-034 model
        gate that must know whether get_or_create_session would create) can reuse it.
        """
        ttl = get_settings().session_soft_ttl_seconds
        updated = session.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=datetime.UTC)
        return (_now() - updated).total_seconds() > ttl

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
        """Resume an owned, non-expired session or create a new one.

        ``project_id`` (ADR-022), ``assistant_mode`` (ADR-012), ``dialog_mode`` (ADR-055), the
        auto-generated ``title`` (chats/03), ``model`` (ADR-034) and ``workspace_project_id``
        (ADR-036) are fixed at creation only — a single source of truth, never re-written here for
        an existing session (rename is handled by the chats module). ``dialog_mode`` defaults to
        ``'smart'`` and is validated (membership + provider-gate) by the caller before creation.
        ``project_id=None`` creates a «чистый чат» session
        (``chat_sessions.project_id = NULL``; ``site.*`` tools not offered). ``model=None`` stores
        ``chat_sessions.model = NULL`` (= the instance default model, resolved by the client at
        generation time — ADR-034 §3). ``workspace_project_id=None`` creates a chat without a
        workspace (``chat_sessions.workspace_project_id = NULL`` — ADR-036; NOT the website-builder
        ``project_id``). Ownership of the workspace is validated by the caller before creation.
        """
        if session_id is not None:
            existing = await self.get_session(session_id, user_id)
            if existing is not None and not self.is_expired(existing):
                return SessionContext(session=existing, is_new=False)
            # Missing or expired → new session (mode/assistant_mode/title fixed at creation).
        new_session = ChatSession(
            user_id=user_id,
            project_id=project_id,
            mode=mode,
            assistant_mode=assistant_mode,
            dialog_mode=dialog_mode,
            title=title,
            model=model,
            workspace_project_id=workspace_project_id,
        )
        self._session.add(new_session)
        await self._session.flush()
        return SessionContext(session=new_session, is_new=True)

    async def touch_session(self, session: ChatSession) -> None:
        session.updated_at = _now()
        await self._session.flush()

    async def add_step(
        self,
        *,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        role: str,
        payload: dict[str, Any],
        usage: dict[str, Any] | None = None,
    ) -> ChatStep:
        step = ChatStep(
            session_id=session_id,
            message_step_id=message_step_id,
            role=role,
            payload=payload,
            usage=usage,
        )
        self._session.add(step)
        await self._session.flush()
        return step

    async def list_steps(self, session_id: uuid.UUID) -> list[ChatStep]:
        # ADR-021: order by the monotonic `seq` (insertion order), NOT (created_at, id).
        # In the server-side tool-loop tool_use + tool_result are written in one transaction →
        # equal transaction-time created_at; the UUID-id tie-break is random and could place
        # tool_result before its tool_use → orphan tool_result → Anthropic 400 (BUG-5). `seq`
        # guarantees tool_use < tool_result by insertion order.
        return list(
            await self._session.scalars(
                select(ChatStep)
                .where(ChatStep.session_id == session_id)
                .order_by(ChatStep.seq.asc())
            )
        )

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
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_tool_call(self, tool_call_id: uuid.UUID) -> ToolCall | None:
        return await self._session.get(ToolCall, tool_call_id)

    async def list_tool_calls_for_step(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> list[ToolCall]:
        """All tool_calls of one assistant turn (ADR-025 barrier). Single query, no N+1.

        Ordered by creation order (id is insertion-stable enough here; the orchestrator filters
        client-side rows and checks their status to decide whether the barrier is closed).

        ``populate_existing=True`` (CRITICAL identity-map fix): ``complete_tool_call()`` flips the
        status with a raw SQL ``UPDATE ... RETURNING`` that does NOT touch the ORM identity-map, so
        ToolCall rows already loaded earlier in this session (e.g. via ``get_tool_call`` in
        ``tool_result``) keep their stale ``status='pending'``. Without this option the barrier
        SELECT would re-return those cached objects unchanged and the continuation would never run.
        ``populate_existing`` forces the freshly-SELECTed DB values (status='completed'/'errored')
        to overwrite the cached attributes, so the barrier sees the actual statuses.
        """
        return list(
            await self._session.scalars(
                select(ToolCall)
                .where(
                    ToolCall.session_id == session_id,
                    ToolCall.message_step_id == message_step_id,
                )
                .order_by(ToolCall.created_at.asc(), ToolCall.id.asc())
                .execution_options(populate_existing=True)
            )
        )

    async def complete_tool_call(
        self,
        *,
        tool_call_id: uuid.UUID,
        status: str,
        result: dict[str, Any] | None,
    ) -> bool:
        """Atomic pending → completed/errored. True if this call performed the transition.

        The raw SQL ``UPDATE`` bypasses the ORM identity-map: any ToolCall instance already loaded
        in this session keeps a stale ``status='pending'``. The freshness guarantee the ADR-025
        barrier relies on is provided by ``list_tool_calls_for_step`` (``populate_existing=True``),
        which re-populates the exact rows the barrier reads from the DB. We intentionally do NOT
        ``expire`` the cached instance here: an expired ToolCall would lazy-refresh on the next
        attribute access (e.g. the audit step reading ``tool_name``/``id`` right after this call),
        and that synchronous refresh outside a greenlet context raises ``MissingGreenlet`` in the
        async engine.
        """
        updated = await self._session.scalar(
            text(
                "UPDATE tool_calls SET status = :status, result = CAST(:result AS JSONB), "
                "completed_at = now() WHERE id = :id AND status = 'pending' RETURNING id"
            ),
            {
                "status": status,
                "result": _json_or_null(result),
                "id": str(tool_call_id),
            },
        )
        return updated is not None

    async def truncate_from_message_step(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> int | None:
        """Truncate session history from a turn for edit+regenerate (ADR-040 §2).

        Deletes the user-step identified by ``message_step_id`` and EVERYTHING after it (its
        assistant/tool steps and all later turns), plus the ``tool_calls`` of the truncated turns.
        Returns the number of deleted ``chat_steps`` (>= 1 when the anchor is found), or ``None``
        when no ``role='user'`` step with ``message_step_id`` exists in the session (caller →
        404 message_not_found).

        - **anchor** = min ``chat_steps.seq`` of the session's step with this ``message_step_id``
          AND ``role='user'`` (ADR-021 monotonic seq is the only reliable order key; ADR-040 §4в:
          the anchor is matched STRICTLY by ``role='user'`` — an assistant/tool-only message_step_id
          resolves to None → 404). None → return None.
        - ``tool_calls`` of the truncated turns are deleted EXPLICITLY (ADR-040 §2 step 3): their FK
          is on ``chat_sessions`` (session_id), NOT on ``chat_steps`` — deleting steps does NOT
          cascade them, so without this they would be orphaned. The subquery reads the
          STILL-EXISTING steps (seq >= anchor) before the steps are deleted.
        - All DELETEs are scoped by ``session_id`` (an already ownership-checked session — the
          caller only truncates a resumed, owned session, ADR-040 §5). No cross-session deletion.
        - ``flush()`` only — the surrounding /chat/run request transaction commits as one unit with
          the new turn's generation (ADR-040 §2), so truncation + new user-step are atomic.
        """
        anchor = await self._session.scalar(
            text(
                "SELECT min(seq) FROM chat_steps "
                "WHERE session_id = :sid AND message_step_id = :msid AND role = 'user'"
            ),
            {"sid": str(session_id), "msid": str(message_step_id)},
        )
        if anchor is None:
            return None
        # Delete tool_calls of the truncated turns FIRST (FK is on chat_sessions, not chat_steps →
        # no cascade). The subquery reads the still-existing chat_steps (seq >= anchor).
        await self._session.execute(
            text(
                "DELETE FROM tool_calls WHERE session_id = :sid AND message_step_id IN ("
                "SELECT DISTINCT message_step_id FROM chat_steps "
                "WHERE session_id = :sid AND seq >= :anchor)"
            ),
            {"sid": str(session_id), "anchor": anchor},
        )
        result = await self._session.execute(
            text("DELETE FROM chat_steps WHERE session_id = :sid AND seq >= :anchor RETURNING id"),
            {"sid": str(session_id), "anchor": anchor},
        )
        deleted = len(result.fetchall())
        await self._session.flush()
        return deleted

    async def assistant_tool_step_id(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> uuid.UUID | None:
        """ADR-025: id of the assistant step carrying the current turn's tool_use blocks.

        For a status=tool_call response on /chat/tool-result with the barrier still open, stepId
        must point at the assistant step whose payload holds the (still-pending) tool_use blocks —
        the latest assistant step of this turn (greatest ``seq``). Returns None if absent.
        """
        step_id: uuid.UUID | None = await self._session.scalar(
            select(ChatStep.id)
            .where(
                ChatStep.session_id == session_id,
                ChatStep.message_step_id == message_step_id,
                ChatStep.role == "assistant",
            )
            .order_by(ChatStep.seq.desc())
            .limit(1)
        )
        return step_id

    async def next_step_after(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID, after_tool_call: uuid.UUID
    ) -> ChatStep | None:
        """For idempotent replay: the assistant step persisted right after a completed tool-result.

        ADR-021: anchored to the monotonic ``seq``, NOT ``created_at``. The tool step recording
        this tool-call's tool_result has a deterministic ``seq``; the next assistant step in this
        message-step with a strictly greater ``seq`` is the round's continuation. ``created_at`` is
        unreliable as an order key (transaction-time ``now()`` is equal for steps of one
        transaction; the UUID-id tie-break is random), so it is not used here.

        Multi-round tool-loop safe: a later round's assistant step has a greater ``seq`` than this
        round's tool step, but the FIRST (smallest seq) assistant step after the anchor is this
        round's step (ASC ``.first()``). Falls back to the latest assistant step (max seq) if the
        anchor tool step is unavailable.
        """
        anchor_seq = await self._session.scalar(
            select(ChatStep.seq)
            .where(
                ChatStep.session_id == session_id,
                ChatStep.message_step_id == message_step_id,
                ChatStep.role == "tool",
                ChatStep.payload["toolCallId"].astext == str(after_tool_call),
            )
            .order_by(ChatStep.seq.asc())
            .limit(1)
        )
        query = select(ChatStep).where(
            ChatStep.session_id == session_id,
            ChatStep.message_step_id == message_step_id,
            ChatStep.role == "assistant",
        )
        if anchor_seq is not None:
            rows = await self._session.scalars(
                query.where(ChatStep.seq > anchor_seq).order_by(ChatStep.seq.asc())
            )
            return rows.first()
        rows = await self._session.scalars(query.order_by(ChatStep.seq.desc()))
        return rows.first()


def _json_or_null(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    import json

    return json.dumps(value)
