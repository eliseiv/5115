"""Chats persistence: read history + metadata edits over chat_sessions/chat_steps/tool_calls.

Read-only over the orchestrator-owned step tables (chats/03 invariant): this module NEVER
writes chat_steps/tool_calls. It only updates chat_sessions metadata (title/is_pinned/
updated_at) and deletes sessions (cascade removes steps/tool_calls via FK). All queries are
scoped ``WHERE user_id = :sub``; a foreign chat is indistinguishable from a missing one (404).
"""

from __future__ import annotations

import datetime
import re
import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, Integer, and_, delete, func, or_, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.chats.cursor import ChatCursor
from app.models import ChatSession, ChatStep, ToolCall

_PREVIEW_MAX_CHARS = 160

# ADR-042: single source of truth for stripping the leading ADR-037 conversation-settings block
# from user-facing output (history + preview). The anchor matches EXACTLY the block that the server
# itself emits in orchestrator._render_context_block (ADR-037 §3) and joins via _compose_turn0_text
# (ADR-039 §3). It is applied at the serialization boundary only — stored chat_steps.payload and the
# model replay (_build_messages) are NEVER touched, so generation is unchanged (no migration).
#   - "block\n\nmsg" → "msg"  (normal turn, _compose_turn0_text → f"{block}\n\n{msg}")
#   - "block"        → ""      (image-only/empty message: _compose_turn0_text returned `block`)
# The suffix `(?:\n\n|\Z)` matches either the "\n\n" separator or end-of-text (no trailing sep).
_CONTEXT_BLOCK_RE = re.compile(r"^\[Conversation settings for this message: [^\]]*\](?:\n\n|\Z)")


def strip_context_block(text: str) -> str:
    """Strip the leading ADR-037 conversation-settings block from user-facing text (ADR-042).

    Single source of truth reused by both call-sites — history (``ChatsService._normalize_payload``)
    and preview (``ChatsRepository._preview``). Strips strictly at the start; if the text does not
    begin with the server-generated block anchor it is returned unchanged (no-op). Two persisted
    shapes are handled (ADR-037 §`_compose_turn0_text`): ``block + "\\n\\n" + msg`` → ``msg``; and
    the image-only/empty-message edge where the persisted text IS the bare block → ``""``.
    The block content is never logged (ADR-037 §6 / 05-security.md).
    """
    return _CONTEXT_BLOCK_RE.sub("", text, count=1)


@dataclass(frozen=True)
class ChatListItem:
    session: ChatSession
    preview: str | None


@dataclass(frozen=True)
class ChatListPage:
    items: list[ChatListItem]
    next_cursor: str | None


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def _text_from_payload(payload: dict[str, Any]) -> str:
    """Concatenate text blocks of a chat_steps.payload (user/assistant content blocks)."""
    parts: list[str] = []
    for block in payload.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return " ".join(p for p in parts if p).strip()


def _truncate(value: str, limit: int = _PREVIEW_MAX_CHARS) -> str:
    collapsed = " ".join(value.split())
    return collapsed[:limit]


class ChatsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- list / search ----

    async def list_chats(
        self,
        *,
        user_id: uuid.UUID,
        query: str | None,
        cursor: ChatCursor | None,
        limit: int,
        workspace_project_id: uuid.UUID | None = None,
    ) -> ChatListPage:
        """List the user's chats: pinned first, then by recency, keyset-paginated.

        Search ``query`` matches title ILIKE OR the text of the first user step ILIKE
        (chats/03). ``workspace_project_id`` (ADR-036) filters to «чаты проекта» when provided.
        Fetch limit+1 to compute next_cursor without a second count query.
        """
        stmt = select(ChatSession).where(ChatSession.user_id == user_id)

        if workspace_project_id is not None:
            stmt = stmt.where(ChatSession.workspace_project_id == workspace_project_id)

        if query:
            like = f"%{query}%"
            first_user_step = (
                select(ChatStep.session_id)
                .where(
                    ChatStep.session_id == ChatSession.id,
                    ChatStep.role == "user",
                    ChatStep.payload["content"][0]["text"].astext.ilike(like),
                )
                .limit(1)
                .exists()
            )
            stmt = stmt.where(or_(ChatSession.title.ilike(like), first_user_step))

        # is_pinned is a boolean column; SQLAlchemy forbids ordering comparisons (`<`/`>`) on
        # booleans, so cast to int for the keyset ordering (and ORDER BY, to keep the plan/order
        # consistent). pinned DESC ⇒ int(is_pinned) DESC; the id tie-break preserves stability.
        pinned_ord = sa_cast(ChatSession.is_pinned, Integer)

        if cursor is not None:
            # Keyset over (is_pinned DESC, updated_at DESC, id DESC): rows strictly "after".
            cursor_pinned = int(cursor.is_pinned)
            stmt = stmt.where(
                or_(
                    pinned_ord < cursor_pinned,
                    and_(
                        pinned_ord == cursor_pinned,
                        ChatSession.updated_at < cursor.updated_at,
                    ),
                    and_(
                        pinned_ord == cursor_pinned,
                        ChatSession.updated_at == cursor.updated_at,
                        ChatSession.id < cursor.id,
                    ),
                )
            )

        stmt = stmt.order_by(
            pinned_ord.desc(),
            ChatSession.updated_at.desc(),
            ChatSession.id.desc(),
        ).limit(limit + 1)

        rows = list(await self._session.scalars(stmt))
        has_more = len(rows) > limit
        page_rows = rows[:limit]

        items = [
            ChatListItem(session=row, preview=await self._preview(row.id)) for row in page_rows
        ]
        next_cursor: str | None = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = ChatCursor(
                is_pinned=last.is_pinned, updated_at=last.updated_at, id=last.id
            ).encode()
        return ChatListPage(items=items, next_cursor=next_cursor)

    async def _preview(self, session_id: uuid.UUID) -> str | None:
        """Preview = truncated text of the latest user/assistant step (chats/03)."""
        step = await self._session.scalar(
            select(ChatStep)
            .where(
                ChatStep.session_id == session_id,
                ChatStep.role.in_(("user", "assistant")),
            )
            .order_by(ChatStep.created_at.desc(), ChatStep.id.desc())
            .limit(1)
        )
        if step is None:
            return None
        text = _text_from_payload(step.payload)
        # ADR-042: for a user step, strip the leading ADR-037 conversation-settings block from the
        # RAW first text block BEFORE _truncate — _truncate collapses "\n\n" → space and would break
        # the anchor. assistant steps carry no block → not touched. Stored payload is unchanged
        # (_text_from_payload reads only). Strip first, then truncate.
        if step.role == "user" and text:
            text = strip_context_block(text)
        return _truncate(text) if text else None

    # ---- single chat ----

    async def get_session(self, session_id: uuid.UUID, user_id: uuid.UUID) -> ChatSession | None:
        row: ChatSession | None = await self._session.scalar(
            select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user_id)
        )
        return row

    async def list_steps(self, session_id: uuid.UUID) -> list[ChatStep]:
        return list(
            await self._session.scalars(
                select(ChatStep)
                .where(ChatStep.session_id == session_id)
                .order_by(ChatStep.created_at, ChatStep.id)
            )
        )

    async def latest_message_step_id(self, session_id: uuid.UUID) -> uuid.UUID | None:
        row: uuid.UUID | None = await self._session.scalar(
            select(ChatStep.message_step_id)
            .where(ChatStep.session_id == session_id)
            .order_by(ChatStep.created_at.desc(), ChatStep.id.desc())
            .limit(1)
        )
        return row

    async def steps_for_message(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> list[ChatStep]:
        return list(
            await self._session.scalars(
                select(ChatStep)
                .where(
                    ChatStep.session_id == session_id,
                    ChatStep.message_step_id == message_step_id,
                )
                .order_by(ChatStep.created_at, ChatStep.id)
            )
        )

    async def tool_calls_for_message(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> dict[uuid.UUID, ToolCall]:
        rows = await self._session.scalars(
            select(ToolCall).where(
                ToolCall.session_id == session_id,
                ToolCall.message_step_id == message_step_id,
            )
        )
        return {row.id: row for row in rows}

    async def provider_id_to_domain_id(self, session_id: uuid.UUID) -> dict[str, uuid.UUID]:
        """Map provider_tool_use_id (raw ``toolu_...``) → domain tool_calls.id for a session.

        ADR-024: built with a SINGLE query per session (no N+1) so history payload normalization
        can resolve every tool_use/tool_result block's id without a per-block lookup. Only the two
        id columns are selected (no full ORM rows needed for the map).
        """
        rows = await self._session.execute(
            select(ToolCall.provider_tool_use_id, ToolCall.id).where(
                ToolCall.session_id == session_id
            )
        )
        return dict(rows.tuples().all())

    # ---- mutations (metadata only) ----

    async def update_metadata(
        self,
        session: ChatSession,
        *,
        title: str | None = None,
        set_title: bool = False,
        is_pinned: bool | None = None,
        set_workspace_project_id: bool = False,
        workspace_project_id: uuid.UUID | None = None,
    ) -> ChatSession:
        if set_title:
            session.title = title
        if is_pinned is not None:
            session.is_pinned = is_pinned
        # ADR-038: partial update — only touch the workspace binding when the field was present in
        # the PATCH body (set_workspace_project_id). A None value with the flag set means "unbind"
        # (workspace_project_id = NULL); absent → flag False → title/is_pinned are not clobbered.
        if set_workspace_project_id:
            session.workspace_project_id = workspace_project_id
        session.updated_at = _now()
        await self._session.flush()
        await self._session.commit()
        return session

    async def delete_session(self, session_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Delete the owned session (FK-cascade drops steps/tool_calls). True if it existed."""
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(ChatSession).where(
                    ChatSession.id == session_id, ChatSession.user_id == user_id
                )
            ),
        )
        await self._session.commit()
        return (result.rowcount or 0) > 0

    async def count_owned(self, user_id: uuid.UUID) -> int:  # pragma: no cover - helper
        return int(
            await self._session.scalar(
                select(func.count()).select_from(ChatSession).where(ChatSession.user_id == user_id)
            )
            or 0
        )
