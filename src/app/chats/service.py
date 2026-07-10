"""Chats use-cases: list/get/steps-view/rename-pin/delete (chats/03-architecture.md).

Owner isolation is enforced at the repository (WHERE user_id = sub); a missing/foreign chat
raises NotFoundError → 404 (BR-CH-1, never reveal foreign existence). steps-view exposes only
domain tool names and human summaries — NEVER raw provider tool_use.id (ADR-008).
"""

from __future__ import annotations

import copy
import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from app.chat.tools import UnknownToolNameError, to_domain_tool_name
from app.chats.cursor import ChatCursor, InvalidCursorError
from app.chats.repository import ChatsRepository, strip_context_block
from app.errors import NotFoundError, ValidationFailedError, WorkspaceNotFoundError
from app.models import ChatSession, ChatStep, ToolCall
from app.workspaces.service import WorkspacesService

logger = logging.getLogger("app.chats.service")

_SUMMARY_MAX_CHARS = 120


@dataclass(frozen=True)
class ChatListItemView:
    id: uuid.UUID
    title: str | None
    preview: str | None
    assistant_mode: str
    is_pinned: bool
    # ADR-028 Решение 1: website-builder project key (= chat_sessions.project_id, ADR-022).
    # null = «чистый чат» (сессия создана без projectId). Независимо от workspace_project_id.
    project_id: str | None
    workspace_project_id: uuid.UUID | None
    updated_at: datetime.datetime


@dataclass(frozen=True)
class ChatListView:
    items: list[ChatListItemView]
    next_cursor: str | None


@dataclass(frozen=True)
class ChatStepView:
    id: uuid.UUID
    message_step_id: uuid.UUID
    role: str
    payload: dict[str, Any]
    usage: dict[str, Any] | None
    created_at: datetime.datetime


@dataclass(frozen=True)
class ChatHistoryView:
    id: uuid.UUID
    title: str | None
    assistant_mode: str
    mode: str
    steps: list[ChatStepView]


@dataclass(frozen=True)
class StepsViewStep:
    kind: str  # reasoning | tool_call | tool_result | assistant_message
    tool_name: str | None
    summary: str
    created_at: datetime.datetime


@dataclass(frozen=True)
class StepsView:
    message_step_id: uuid.UUID
    step_count: int
    steps: list[StepsViewStep]


def _truncate(value: str, limit: int = _SUMMARY_MAX_CHARS) -> str:
    collapsed = " ".join(value.split())
    return collapsed[:limit]


def _text_summary(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in payload.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return _truncate(" ".join(p for p in parts if p))


class ChatsService:
    def __init__(self, repo: ChatsRepository, workspaces: WorkspacesService | None = None) -> None:
        self._repo = repo
        # ADR-038: read-only workspace ownership check for PATCH workspace re-binding. Optional so
        # read-only call-sites (list/history/steps) need not wire it; required for set_workspace.
        self._workspaces = workspaces

    async def list_chats(
        self,
        *,
        user_id: uuid.UUID,
        query: str | None,
        cursor: str | None,
        limit: int,
        workspace_project_id: uuid.UUID | None = None,
    ) -> ChatListView:
        decoded: ChatCursor | None = None
        if cursor:
            try:
                decoded = ChatCursor.decode(cursor)
            except InvalidCursorError as exc:
                raise ValidationFailedError("invalid cursor") from exc

        page = await self._repo.list_chats(
            user_id=user_id,
            query=query,
            cursor=decoded,
            limit=limit,
            workspace_project_id=workspace_project_id,
        )
        items = [
            ChatListItemView(
                id=item.session.id,
                title=item.session.title,
                preview=item.preview,
                assistant_mode=item.session.assistant_mode,
                is_pinned=item.session.is_pinned,
                # ADR-028: website-builder project key from the session (free string, ADR-022).
                project_id=item.session.project_id,
                # ADR-036: real workspace binding from the session (NULL = chat without workspace).
                workspace_project_id=item.session.workspace_project_id,
                updated_at=item.session.updated_at,
            )
            for item in page.items
        ]
        return ChatListView(items=items, next_cursor=page.next_cursor)

    async def _require_session(self, session_id: uuid.UUID, user_id: uuid.UUID) -> ChatSession:
        session = await self._repo.get_session(session_id, user_id)
        if session is None:
            raise NotFoundError("chat not found")
        return session

    async def get_history(self, session_id: uuid.UUID, user_id: uuid.UUID) -> ChatHistoryView:
        session = await self._require_session(session_id, user_id)
        steps = await self._repo.list_steps(session_id)
        # ADR-024: build the provider_tool_use_id → domain tool_calls.id map ONCE per session (one
        # query, no N+1). Every tool_use/tool_result block of every step resolves its public id
        # through this map; provider ids (toolu_...) never surface in the history response.
        provider_to_domain = await self._repo.provider_id_to_domain_id(session_id)
        return ChatHistoryView(
            id=session.id,
            title=session.title,
            assistant_mode=session.assistant_mode,
            mode=session.mode,
            steps=[
                ChatStepView(
                    id=step.id,
                    message_step_id=step.message_step_id,
                    role=step.role,
                    payload=self._normalize_payload(step, provider_to_domain),
                    usage=step.usage,
                    created_at=step.created_at,
                )
                for step in steps
            ],
        )

    @staticmethod
    def _normalize_payload(
        step: ChatStep, provider_to_domain: dict[str, uuid.UUID]
    ) -> dict[str, Any]:
        """Normalize a step's stored wire payload to the domain view for the history response.

        ADR-024 — applied ONLY at the serialization boundary, on a DEEP COPY: the stored
        ``chat_steps.payload`` MUST stay wire-valid for Anthropic replay (underscore tool names,
        provider ``toolu_...`` ids) and is never mutated here. On the copy:
        - ``providerToolUseId`` (internal, ADR-008) is dropped from the (tool) step payload;
        - ``tool_use`` blocks: ``name`` underscore→dot via ``to_domain_tool_name``; ``id``
          (``toolu_...``) → domain ``tool_calls.id`` via the session map;
        - ``tool_result`` blocks: ``tool_use_id`` (``toolu_...``) → the same domain id;
        - ``text`` blocks and ``tool_use.input`` are left byte-for-byte unchanged.
        Defensive (history is read-only, never 500): an unknown tool name or a provider id absent
        from the map is left as-is and a WARNING is logged (BUG-4 invariant says tool_calls cover
        every tool_use, so this is an upstream anomaly, not a normal path).
        """
        payload = copy.deepcopy(step.payload)
        # ADR-008: never expose the raw provider id stored on tool steps.
        payload.pop("providerToolUseId", None)
        content = payload.get("content")
        if not isinstance(content, list):
            return payload
        # ADR-042: for a user step, strip the leading ADR-037 conversation-settings block from the
        # FIRST text block (where the block leads, ADR-037 §4) — on this deep copy only, so the
        # stored payload stays wire-valid for replay. assistant/tool steps carry no block → skip.
        if step.role == "user":
            ChatsService._strip_leading_context_block(content)
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                ChatsService._normalize_tool_use_block(block, provider_to_domain, step)
            elif block_type == "tool_result":
                ChatsService._normalize_tool_result_block(block, provider_to_domain, step)
            # text blocks and tool_use.input are intentionally left unchanged.
        return payload

    @staticmethod
    def _strip_leading_context_block(content: list[Any]) -> None:
        """Strip the ADR-037 block from the first text block of a user step's content (ADR-042).

        Mutates the FIRST ``type == "text"`` block in place (on the deep copy from
        ``_normalize_payload``). Only the leading text block carries the block (ADR-037 §4);
        attachment placeholders and any later blocks are not touched.
        """
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block["text"] = strip_context_block(str(block.get("text", "")))
                return

    @staticmethod
    def _normalize_tool_use_block(
        block: dict[str, Any], provider_to_domain: dict[str, uuid.UUID], step: ChatStep
    ) -> None:
        raw_name = block.get("name")
        if isinstance(raw_name, str):
            try:
                block["name"] = to_domain_tool_name(raw_name)
            except UnknownToolNameError:
                logger.warning(
                    "history payload: unknown tool name in tool_use block (left as-is)",
                    extra={"sessionId": str(step.session_id), "stepId": str(step.id)},
                )
        provider_id = block.get("id")
        domain_id = provider_to_domain.get(provider_id) if isinstance(provider_id, str) else None
        if domain_id is not None:
            block["id"] = str(domain_id)
        elif provider_id is not None:
            logger.warning(
                "history payload: provider tool_use.id not found in tool_calls map (left as-is)",
                extra={"sessionId": str(step.session_id), "stepId": str(step.id)},
            )

    @staticmethod
    def _normalize_tool_result_block(
        block: dict[str, Any], provider_to_domain: dict[str, uuid.UUID], step: ChatStep
    ) -> None:
        provider_id = block.get("tool_use_id")
        domain_id = provider_to_domain.get(provider_id) if isinstance(provider_id, str) else None
        if domain_id is not None:
            block["tool_use_id"] = str(domain_id)
        elif provider_id is not None:
            logger.warning(
                "history payload: provider tool_use_id not found in tool_calls map (left as-is)",
                extra={"sessionId": str(step.session_id), "stepId": str(step.id)},
            )

    async def steps_view(
        self,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        message_step_id: uuid.UUID | None,
    ) -> StepsView:
        await self._require_session(session_id, user_id)
        target = message_step_id or await self._repo.latest_message_step_id(session_id)
        if target is None:
            raise NotFoundError("no steps for chat")

        steps = await self._repo.steps_for_message(session_id, target)
        if not steps:
            raise NotFoundError("message step not found")
        tool_calls = await self._repo.tool_calls_for_message(session_id, target)
        # Map raw provider tool_use.id → domain tool name so assistant tool_use blocks (which
        # carry the raw anthropic name/id) resolve to the public dotted name without exposing
        # the raw id (ADR-008).
        by_provider_id = {tc.provider_tool_use_id: tc for tc in tool_calls.values()}

        view_steps: list[StepsViewStep] = []
        for step in steps:
            view_steps.extend(self._render_step(step, tool_calls, by_provider_id))
        return StepsView(
            message_step_id=target,
            step_count=len(view_steps),
            steps=view_steps,
        )

    @staticmethod
    def _render_step(
        step: ChatStep,
        tool_calls: dict[uuid.UUID, ToolCall],
        by_provider_id: dict[str, ToolCall],
    ) -> list[StepsViewStep]:
        """Flatten one chat_step into UI steps (reasoning/tool_call/tool_result/assistant_message).

        Never emits secrets or raw provider tool_use.id — only domain tool names (with a dot)
        and short human summaries (ADR-008, chats/06-rbac).
        """
        out: list[StepsViewStep] = []
        if step.role == "assistant":
            content = step.payload.get("content", [])
            has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
            summary = _text_summary(step.payload)
            if summary:
                out.append(
                    StepsViewStep(
                        kind="reasoning" if has_tool_use else "assistant_message",
                        tool_name=None,
                        summary=summary,
                        created_at=step.created_at,
                    )
                )
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    # Resolve the public dotted tool name via the raw provider id (never emitted).
                    match = by_provider_id.get(str(block.get("id")))
                    domain_name = match.tool_name if match is not None else None
                    out.append(
                        StepsViewStep(
                            kind="tool_call",
                            tool_name=domain_name,
                            summary=(
                                f"вызов {domain_name}" if domain_name else "вызов инструмента"
                            ),
                            created_at=step.created_at,
                        )
                    )
        elif step.role == "tool":
            tool_call_id = step.payload.get("toolCallId")
            tool_name = step.payload.get("toolName")
            if tool_name is None and tool_call_id is not None:
                try:
                    match = tool_calls.get(uuid.UUID(str(tool_call_id)))
                except ValueError:
                    match = None
                tool_name = match.tool_name if match is not None else None
            out.append(
                StepsViewStep(
                    kind="tool_result",
                    tool_name=tool_name,
                    summary=f"результат {tool_name}" if tool_name else "результат инструмента",
                    created_at=step.created_at,
                )
            )
        # role == "user" is not part of the assistant step-view (it is the trigger, not a step).
        return out

    async def rename_or_pin(
        self,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        title: str | None,
        set_title: bool,
        is_pinned: bool | None,
        set_workspace_project_id: bool = False,
        workspace_project_id: uuid.UUID | None = None,
    ) -> ChatSession:
        """Patch chat metadata: rename/pin and/or (un)bind the workspace (ADR-038).

        Owner isolation: a missing/foreign chat → 404 (``_require_session``). When the workspace
        binding is set to a uuid, the target workspace MUST belong to the same user → otherwise
        404 ``workspace_not_found`` (consistent with ``/chat/run``, ADR-036 §3); ``null`` unbinds
        without touching the workspaces service. Idempotent: re-setting the same value is allowed.
        """
        session = await self._require_session(session_id, user_id)
        if set_workspace_project_id and workspace_project_id is not None:
            if self._workspaces is None:  # pragma: no cover - always wired via DI for PATCH
                raise RuntimeError("workspaces service not configured for workspace re-binding")
            if not await self._workspaces.owns_workspace(workspace_project_id, user_id):
                raise WorkspaceNotFoundError("workspace not found")
        return await self._repo.update_metadata(
            session,
            title=title,
            set_title=set_title,
            is_pinned=is_pinned,
            set_workspace_project_id=set_workspace_project_id,
            workspace_project_id=workspace_project_id,
        )

    async def delete_chat(self, session_id: uuid.UUID, user_id: uuid.UUID) -> None:
        deleted = await self._repo.delete_session(session_id, user_id)
        if not deleted:
            # Idempotent: a missing/foreign chat (incl. already-deleted) → 404 (chats/02).
            raise NotFoundError("chat not found")
