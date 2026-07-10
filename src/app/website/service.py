"""Website Service (WB-3): project resolution + site_files CRUD with limits & path guard.

Storage backend is PostgreSQL on the start (site_files.content BYTEA, TD-009). The public
methods here are the single contract used by the site.* tool handlers and the preview endpoint;
swapping the storage backend (DB -> object-storage) must not change these signatures (ADR-010).

userId / external_project_id are always passed in by the caller from the SESSION context — never
from model-supplied tool args — so the model cannot write into another user's project (IDOR
guard, website-builder/05-security.md).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PREVIEW_CONTENT_TYPE_ALLOWLIST, get_settings
from app.models import Project, SiteFile
from app.website.paths import InvalidPathError, normalize_site_path


class SiteFileError(Exception):
    """Domain error for site-file operations, with a machine-readable code for tool is_error.

    Codes (website-builder/02-api-contracts.md): invalid_path, invalid_content_type,
    file_too_large, project_too_large, too_many_files, file_not_found, invalid_encoding.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class StoredFileMeta:
    path: str
    content_type: str
    size: int


@dataclass(frozen=True)
class WriteResult:
    path: str
    bytes_written: int
    file_count: int
    project_bytes: int


@dataclass(frozen=True)
class ProjectStats:
    file_count: int
    project_bytes: int


@dataclass(frozen=True)
class FileContent:
    path: str
    content: bytes
    content_type: str
    size: int


class WebsiteService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- project resolution ----

    async def resolve_project(self, *, user_id: uuid.UUID, external_project_id: str) -> Project:
        """Idempotent upsert of the project by (user_id, external_project_id).

        Uses ux_projects_user_external. Creates the row if absent, returns the existing one
        otherwise. user_id comes from the session (not args), so the project always belongs to the
        authenticated owner.
        """
        await self._session.execute(
            text(
                "INSERT INTO projects (user_id, external_project_id) VALUES (:uid, :ext) "
                "ON CONFLICT (user_id, external_project_id) DO NOTHING"
            ),
            {"uid": str(user_id), "ext": external_project_id},
        )
        project = await self._session.scalar(
            select(Project).where(
                Project.user_id == user_id,
                Project.external_project_id == external_project_id,
            )
        )
        if project is None:  # pragma: no cover - defensive; the upsert just guaranteed a row
            raise SiteFileError("project_unresolved", "project could not be resolved")
        return project

    async def get_existing_project(
        self, *, user_id: uuid.UUID, external_project_id: str
    ) -> Project | None:
        """Read-only lookup of an existing project (no creation). For read/list/delete/preview."""
        project: Project | None = await self._session.scalar(
            select(Project).where(
                Project.user_id == user_id,
                Project.external_project_id == external_project_id,
            )
        )
        return project

    async def get_project_by_id(self, project_id: uuid.UUID) -> Project | None:
        return await self._session.get(Project, project_id)

    # ---- stats / limits ----

    async def _project_stats(self, project_id: uuid.UUID) -> ProjectStats:
        row = (
            await self._session.execute(
                select(
                    func.count(SiteFile.id),
                    func.coalesce(func.sum(SiteFile.size), 0),
                ).where(SiteFile.project_id == project_id)
            )
        ).one()
        return ProjectStats(file_count=int(row[0]), project_bytes=int(row[1]))

    # ---- CRUD ----

    async def write_file(
        self,
        *,
        project: Project,
        path: str,
        content: bytes,
        content_type: str,
    ) -> WriteResult:
        """Upsert a file by (project_id, normalized_path) with limit & allowlist checks.

        Validation order (all BEFORE the write): content-type allowlist, path guard, per-file
        size, then aggregate limits (project bytes / file count) computed against the delta of
        replacing any existing file at the same path. Any violation raises SiteFileError (the
        caller maps it to tool is_error, not a 5xx).
        """
        settings = get_settings()
        if content_type not in PREVIEW_CONTENT_TYPE_ALLOWLIST:
            raise SiteFileError("invalid_content_type", f"content_type not allowed: {content_type}")
        try:
            normalized = normalize_site_path(path)
        except InvalidPathError as exc:
            raise SiteFileError("invalid_path", str(exc)) from exc

        size = len(content)
        if size > settings.preview_max_file_bytes:
            raise SiteFileError("file_too_large", "file exceeds the per-file size limit")

        existing = await self._session.scalar(
            select(SiteFile).where(SiteFile.project_id == project.id, SiteFile.path == normalized)
        )
        stats = await self._project_stats(project.id)

        # Compute post-write aggregates considering replacement of an existing file at this path.
        prior_size = int(existing.size) if existing is not None else 0
        projected_bytes = stats.project_bytes - prior_size + size
        projected_count = stats.file_count + (1 if existing is None else 0)

        if projected_bytes > settings.preview_max_project_bytes:
            raise SiteFileError("project_too_large", "project exceeds the total size limit")
        if projected_count > settings.preview_max_files:
            raise SiteFileError("too_many_files", "project exceeds the maximum number of files")

        if existing is not None:
            existing.content = content
            existing.content_type = content_type
            existing.size = size
            existing.updated_at = func.now()
        else:
            self._session.add(
                SiteFile(
                    project_id=project.id,
                    path=normalized,
                    content=content,
                    content_type=content_type,
                    size=size,
                )
            )
        await self._session.flush()

        post = await self._project_stats(project.id)
        return WriteResult(
            path=normalized,
            bytes_written=size,
            file_count=post.file_count,
            project_bytes=post.project_bytes,
        )

    async def list_files(self, project: Project) -> tuple[list[StoredFileMeta], ProjectStats]:
        rows = list(
            await self._session.scalars(
                select(SiteFile)
                .where(SiteFile.project_id == project.id)
                .order_by(SiteFile.path.asc())
            )
        )
        metas = [
            StoredFileMeta(path=r.path, content_type=r.content_type, size=int(r.size)) for r in rows
        ]
        stats = await self._project_stats(project.id)
        return metas, stats

    async def read_file(self, *, project_id: uuid.UUID, path: str) -> FileContent | None:
        """Read a file by (project_id, normalized_path). Returns None if path invalid or absent."""
        try:
            normalized = normalize_site_path(path)
        except InvalidPathError:
            return None
        row = await self._session.scalar(
            select(SiteFile).where(SiteFile.project_id == project_id, SiteFile.path == normalized)
        )
        if row is None:
            return None
        return FileContent(
            path=row.path,
            content=bytes(row.content),
            content_type=row.content_type,
            size=int(row.size),
        )

    async def delete_file(self, *, project: Project, path: str) -> tuple[bool, ProjectStats]:
        """Delete a file by (project_id, normalized_path). Returns (deleted, post-delete stats)."""
        try:
            normalized = normalize_site_path(path)
        except InvalidPathError as exc:
            raise SiteFileError("invalid_path", str(exc)) from exc
        row = await self._session.scalar(
            select(SiteFile).where(SiteFile.project_id == project.id, SiteFile.path == normalized)
        )
        deleted = False
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()
            deleted = True
        stats = await self._project_stats(project.id)
        return deleted, stats
