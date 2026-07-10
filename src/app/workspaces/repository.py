"""Workspaces persistence over workspace_projects/workspace_files (workspaces/03-architecture.md).

All queries are scoped ``WHERE user_id = :sub`` (workspace) or via the owning workspace (files), so
a foreign workspace/file is indistinguishable from a missing one (404 at the service layer). The
chat-binding column ``chat_sessions.workspace_project_id`` is owned by the chat repository for
writes; this module only counts chats per workspace for the list view (read-only).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatSession, WorkspaceFile, WorkspaceProject
from app.workspaces.cursor import WorkspaceCursor


@dataclass(frozen=True)
class WorkspaceListItem:
    workspace: WorkspaceProject
    file_count: int
    chat_count: int


@dataclass(frozen=True)
class WorkspaceListPage:
    items: list[WorkspaceListItem]
    next_cursor: str | None


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class WorkspacesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- workspace CRUD ----

    async def create_workspace(
        self,
        *,
        user_id: uuid.UUID,
        name: str,
        description: str | None,
        instructions: str | None,
    ) -> WorkspaceProject:
        row = WorkspaceProject(
            user_id=user_id,
            name=name,
            description=description,
            instructions=instructions,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.commit()
        return row

    async def get_workspace(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> WorkspaceProject | None:
        row: WorkspaceProject | None = await self._session.scalar(
            select(WorkspaceProject).where(
                WorkspaceProject.id == workspace_id,
                WorkspaceProject.user_id == user_id,
            )
        )
        return row

    async def get_instructions(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
        """Read ONLY the ``instructions`` column of an owned workspace (ADR-036 §3 continuation).

        Light-weight (single column, no files/content) — used on EVERY ``/chat/tool-result``
        continuation to re-inject the project instructions into ``system`` (the param, unlike
        knowledge files, is not part of the message history). Returns None when the workspace no
        longer exists, is foreign, or its instructions are NULL/empty (caller → base system prompt).
        """
        instructions: str | None = await self._session.scalar(
            select(WorkspaceProject.instructions).where(
                WorkspaceProject.id == workspace_id,
                WorkspaceProject.user_id == user_id,
            )
        )
        return instructions

    async def list_workspaces(
        self, *, user_id: uuid.UUID, cursor: WorkspaceCursor | None, limit: int
    ) -> WorkspaceListPage:
        """List the owner's workspaces by recency (updated_at DESC, id DESC), keyset-paginated.

        Fetch limit+1 to compute next_cursor without a second count query. fileCount/chatCount are
        resolved with two bulk aggregate queries over the page (no N+1).
        """
        stmt = select(WorkspaceProject).where(WorkspaceProject.user_id == user_id)
        if cursor is not None:
            stmt = stmt.where(
                (WorkspaceProject.updated_at < cursor.updated_at)
                | (
                    (WorkspaceProject.updated_at == cursor.updated_at)
                    & (WorkspaceProject.id < cursor.id)
                )
            )
        stmt = stmt.order_by(WorkspaceProject.updated_at.desc(), WorkspaceProject.id.desc()).limit(
            limit + 1
        )

        rows = list(await self._session.scalars(stmt))
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        ids = [row.id for row in page_rows]

        file_counts = await self._file_counts(ids)
        chat_counts = await self._chat_counts(ids)

        items = [
            WorkspaceListItem(
                workspace=row,
                file_count=file_counts.get(row.id, 0),
                chat_count=chat_counts.get(row.id, 0),
            )
            for row in page_rows
        ]
        next_cursor: str | None = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = WorkspaceCursor(updated_at=last.updated_at, id=last.id).encode()
        return WorkspaceListPage(items=items, next_cursor=next_cursor)

    async def _file_counts(self, workspace_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not workspace_ids:
            return {}
        rows = await self._session.execute(
            select(WorkspaceFile.workspace_project_id, func.count())
            .where(WorkspaceFile.workspace_project_id.in_(workspace_ids))
            .group_by(WorkspaceFile.workspace_project_id)
        )
        return {wid: int(count) for wid, count in rows.tuples().all()}

    async def _chat_counts(self, workspace_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not workspace_ids:
            return {}
        rows = await self._session.execute(
            select(ChatSession.workspace_project_id, func.count())
            .where(ChatSession.workspace_project_id.in_(workspace_ids))
            .group_by(ChatSession.workspace_project_id)
        )
        return {wid: int(count) for wid, count in rows.tuples().all() if wid is not None}

    async def update_workspace(
        self,
        workspace: WorkspaceProject,
        *,
        name: str | None,
        set_description: bool,
        description: str | None,
        set_instructions: bool,
        instructions: str | None,
    ) -> WorkspaceProject:
        if name is not None:
            workspace.name = name
        if set_description:
            workspace.description = description
        if set_instructions:
            workspace.instructions = instructions
        workspace.updated_at = _now()
        await self._session.flush()
        await self._session.commit()
        return workspace

    async def delete_workspace(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Delete the owned workspace. True if it existed.

        FK cascade drops workspace_files; chat_sessions.workspace_project_id is set NULL by the FK
        (ON DELETE SET NULL) so the project's chats survive as «чистые» chats (ADR-036 §5).
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(WorkspaceProject).where(
                    WorkspaceProject.id == workspace_id,
                    WorkspaceProject.user_id == user_id,
                )
            ),
        )
        await self._session.commit()
        return (result.rowcount or 0) > 0

    # ---- files ----

    async def list_files(self, workspace_id: uuid.UUID) -> list[WorkspaceFile]:
        """All files of a workspace, oldest first (created_at ASC) — ADR-036 §6 injection order."""
        return list(
            await self._session.scalars(
                select(WorkspaceFile)
                .where(WorkspaceFile.workspace_project_id == workspace_id)
                .order_by(WorkspaceFile.created_at.asc(), WorkspaceFile.id.asc())
            )
        )

    async def file_count(self, workspace_id: uuid.UUID) -> int:
        return int(
            await self._session.scalar(
                select(func.count())
                .select_from(WorkspaceFile)
                .where(WorkspaceFile.workspace_project_id == workspace_id)
            )
            or 0
        )

    async def total_bytes(self, workspace_id: uuid.UUID) -> int:
        return int(
            await self._session.scalar(
                select(func.coalesce(func.sum(WorkspaceFile.size), 0)).where(
                    WorkspaceFile.workspace_project_id == workspace_id
                )
            )
            or 0
        )

    async def add_file(
        self,
        *,
        workspace_id: uuid.UUID,
        filename: str,
        content: bytes,
        media_type: str,
        size: int,
        extracted_text: str | None,
    ) -> WorkspaceFile:
        row = WorkspaceFile(
            workspace_project_id=workspace_id,
            filename=filename,
            content=content,
            media_type=media_type,
            size=size,
            extracted_text=extracted_text,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.commit()
        return row

    async def get_file(self, workspace_id: uuid.UUID, file_id: uuid.UUID) -> WorkspaceFile | None:
        row: WorkspaceFile | None = await self._session.scalar(
            select(WorkspaceFile).where(
                WorkspaceFile.id == file_id,
                WorkspaceFile.workspace_project_id == workspace_id,
            )
        )
        return row

    async def delete_file(self, workspace_id: uuid.UUID, file_id: uuid.UUID) -> bool:
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(WorkspaceFile).where(
                    WorkspaceFile.id == file_id,
                    WorkspaceFile.workspace_project_id == workspace_id,
                )
            ),
        )
        await self._session.commit()
        return (result.rowcount or 0) > 0
