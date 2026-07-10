"""Server-side site.* tool handlers (WB-5, ADR-011).

Executed by the backend inside the chat tool-loop. The owning userId and external_project_id
come from the SESSION context (passed in by the orchestrator), NEVER from model-supplied args
(IDOR guard, website-builder/05-security.md). Each handler returns a ToolExecution: either a
result dict (serialized into the Anthropic tool_result) or an is_error with a machine-readable
code Claude can react to (errors are NOT surfaced as HTTP 5xx, website-builder/02-api-contracts).

Mutating handlers (site.write_file / site.delete) record a tool_mutation audit event in the SAME
DB transaction as the mutation (audit/03-architecture), via the caller's session.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_TOOL_MUTATION, AuditEvent, AuditService
from app.chat.tools import (
    TOOL_SITE_DELETE,
    TOOL_SITE_LIST,
    TOOL_SITE_PREVIEW,
    TOOL_SITE_READ,
    TOOL_SITE_WRITE_FILE,
)
from app.config import get_settings
from app.observability.metrics import site_file_write_total
from app.website.service import SiteFileError, WebsiteService
from app.website.signed_url import PreviewSecretMissingError, build_token

_DEFAULT_ENTRY = "index.html"


@dataclass(frozen=True)
class ToolExecution:
    """Outcome of a server-side tool: a result payload, or an error code + message."""

    result: dict[str, Any] | None
    is_error: bool
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def ok(cls, result: dict[str, Any]) -> ToolExecution:
        return cls(result=result, is_error=False)

    @classmethod
    def error(cls, code: str, message: str) -> ToolExecution:
        return cls(result=None, is_error=True, error_code=code, error_message=message)

    def to_tool_result_payload(self) -> dict[str, Any]:
        """Shape forwarded to the orchestrator as a tool_result (result or error envelope)."""
        if self.is_error:
            return {"error": {"code": self.error_code, "message": self.error_message}}
        return {"result": self.result}


def _decode_content(content: str, encoding: str) -> bytes | None:
    if encoding == "utf8":
        return content.encode("utf-8")
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        return None


class SiteToolHandlers:
    """Dispatch + handlers for server-side site.* tools (ADR-011)."""

    def __init__(self, session: AsyncSession, website: WebsiteService, audit: AuditService) -> None:
        self._session = session
        self._website = website
        self._audit = audit

    async def execute(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        user_id: uuid.UUID,
        external_project_id: str,
        session_id: uuid.UUID,
    ) -> ToolExecution:
        """Execute a site.* tool. user_id/external_project_id are session context, not args."""
        try:
            if tool_name == TOOL_SITE_WRITE_FILE:
                return await self._write_file(args, user_id, external_project_id, session_id)
            if tool_name == TOOL_SITE_PREVIEW:
                return await self._preview(args, user_id, external_project_id)
            if tool_name == TOOL_SITE_LIST:
                return await self._list(user_id, external_project_id)
            if tool_name == TOOL_SITE_READ:
                return await self._read(args, user_id, external_project_id)
            if tool_name == TOOL_SITE_DELETE:
                return await self._delete(args, user_id, external_project_id, session_id)
        except SiteFileError as exc:
            return ToolExecution.error(exc.code, str(exc))
        # Unknown server-side tool name — should never happen (validated upstream).
        return ToolExecution.error("unknown_tool", f"unknown server-side tool: {tool_name}")

    async def _write_file(
        self,
        args: dict[str, Any],
        user_id: uuid.UUID,
        external_project_id: str,
        session_id: uuid.UUID,
    ) -> ToolExecution:
        content = _decode_content(str(args["content"]), str(args["encoding"]))
        if content is None:
            site_file_write_total.labels(result="invalid_encoding").inc()
            return ToolExecution.error("invalid_encoding", "content is not valid base64")
        try:
            project = await self._website.resolve_project(
                user_id=user_id, external_project_id=external_project_id
            )
            result = await self._website.write_file(
                project=project,
                path=str(args["path"]),
                content=content,
                content_type=str(args["contentType"]),
            )
        except SiteFileError as exc:
            site_file_write_total.labels(result=exc.code).inc()
            raise
        # MUTATING → audit tool_mutation in the same transaction as the write (no content body).
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_TOOL_MUTATION,
                payload={
                    "toolName": TOOL_SITE_WRITE_FILE,
                    "projectId": str(project.id),
                    "path": result.path,
                    "bytesWritten": result.bytes_written,
                },
            )
        )
        site_file_write_total.labels(result="success").inc()
        return ToolExecution.ok(
            {
                "path": result.path,
                "bytesWritten": result.bytes_written,
                "fileCount": result.file_count,
                "projectBytes": result.project_bytes,
            }
        )

    async def _preview(
        self, args: dict[str, Any], user_id: uuid.UUID, external_project_id: str
    ) -> ToolExecution:
        project = await self._website.get_existing_project(
            user_id=user_id, external_project_id=external_project_id
        )
        if project is None:
            return ToolExecution.error("project_not_found", "no project to preview")
        entry_raw = args.get("entry")
        entry = str(entry_raw) if entry_raw else _DEFAULT_ENTRY
        try:
            signed = build_token(project_id=project.id, owner_user_id=user_id)
        except PreviewSecretMissingError as exc:
            return ToolExecution.error("preview_unavailable", str(exc))
        # ADR-031: absolute URL on our domain so the model copies it verbatim and does not
        # hallucinate a host. Normalization strips the trailing slash => no double slash before
        # /v1/. Empty SERVICE_DOMAIN (dev) => relative fallback, no hardcoded localhost (TD-022).
        domain = get_settings().normalized_service_domain()
        if domain:
            url = f"https://{domain}/v1/preview/{project.id}/{signed.token}/{entry}"
        else:
            url = f"/v1/preview/{project.id}/{signed.token}/{entry}"
        expires_at = datetime.datetime.fromtimestamp(signed.expires_at, tz=datetime.UTC).isoformat()
        return ToolExecution.ok({"url": url, "expiresAt": expires_at})

    async def _list(self, user_id: uuid.UUID, external_project_id: str) -> ToolExecution:
        project = await self._website.get_existing_project(
            user_id=user_id, external_project_id=external_project_id
        )
        if project is None:
            return ToolExecution.ok({"files": [], "fileCount": 0, "projectBytes": 0})
        metas, stats = await self._website.list_files(project)
        return ToolExecution.ok(
            {
                "files": [
                    {"path": m.path, "contentType": m.content_type, "size": m.size} for m in metas
                ],
                "fileCount": stats.file_count,
                "projectBytes": stats.project_bytes,
            }
        )

    async def _read(
        self, args: dict[str, Any], user_id: uuid.UUID, external_project_id: str
    ) -> ToolExecution:
        project = await self._website.get_existing_project(
            user_id=user_id, external_project_id=external_project_id
        )
        if project is None:
            return ToolExecution.error("file_not_found", "file not found")
        file = await self._website.read_file(project_id=project.id, path=str(args["path"]))
        if file is None:
            return ToolExecution.error("file_not_found", "file not found")
        # Text content types are returned as utf8; everything else as base64.
        if file.content_type.startswith("text/") or file.content_type == "application/json":
            try:
                content = file.content.decode("utf-8")
                encoding = "utf8"
            except UnicodeDecodeError:
                content = base64.b64encode(file.content).decode("ascii")
                encoding = "base64"
        else:
            content = base64.b64encode(file.content).decode("ascii")
            encoding = "base64"
        return ToolExecution.ok(
            {
                "path": file.path,
                "content": content,
                "encoding": encoding,
                "contentType": file.content_type,
                "size": file.size,
            }
        )

    async def _delete(
        self,
        args: dict[str, Any],
        user_id: uuid.UUID,
        external_project_id: str,
        session_id: uuid.UUID,
    ) -> ToolExecution:
        project = await self._website.get_existing_project(
            user_id=user_id, external_project_id=external_project_id
        )
        if project is None:
            return ToolExecution.ok(
                {"path": str(args["path"]), "deleted": False, "fileCount": 0, "projectBytes": 0}
            )
        deleted, stats = await self._website.delete_file(project=project, path=str(args["path"]))
        # MUTATING → audit tool_mutation in the same transaction as the delete.
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_TOOL_MUTATION,
                payload={
                    "toolName": TOOL_SITE_DELETE,
                    "projectId": str(project.id),
                    "path": str(args["path"]),
                    "deleted": deleted,
                },
            )
        )
        return ToolExecution.ok(
            {
                "path": str(args["path"]),
                "deleted": deleted,
                "fileCount": stats.file_count,
                "projectBytes": stats.project_bytes,
            }
        )
