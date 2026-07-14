"""Workspaces use-cases: CRUD + knowledge-file upload/list/delete + context assembly (ADR-036).

Owner isolation is enforced at the repository (WHERE user_id = sub); a missing/foreign workspace or
file raises NotFoundError → 404 (workspaces/06-rbac, never reveal foreign existence). The service
also assembles the model context for the orchestrator (instructions + files) for a workspace chat's
first turn — provider-agnostic (extracted_text as text; images as vision blocks via the client).
"""

from __future__ import annotations

import base64
import datetime
import uuid
from dataclasses import dataclass, field

from app.chat.attachments import PreparedAttachments
from app.config import Settings, get_settings
from app.errors import NotFoundError, ValidationFailedError
from app.models import WorkspaceFile, WorkspaceProject
from app.schemas.workspaces import WorkspaceFileUploadRequest
from app.workspaces.cursor import InvalidCursorError, WorkspaceCursor
from app.workspaces.repository import WorkspaceListPage, WorkspacesRepository
from app.workspaces.text_extract import validate_and_extract

# Image mediaTypes are injected as vision blocks (no extracted_text); kept in sync with the
# attachments allowlist (Q-020-1). Used to branch the per-provider content block in context build.
_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


@dataclass(frozen=True)
class WorkspaceContext:
    """Assembled workspace context for a chat's first turn (ADR-036 §3/§6).

    - ``instructions``: the project system-prompt to inject AFTER the base assistant_mode prompt
      (empty/None → no injection);
    - ``attachments``: a PreparedAttachments carrying the knowledge files as provider content blocks
      (text blocks for document/text via extracted_text; vision blocks for images). None when the
      workspace has no injectable files. Reuses the same client injection path as chat attachments.
    """

    instructions: str | None
    attachments: PreparedAttachments | None = None


@dataclass(frozen=True)
class WorkspaceFileView:
    file_id: uuid.UUID
    filename: str
    media_type: str
    size: int
    has_extracted_text: bool
    created_at: datetime.datetime


@dataclass(frozen=True)
class WorkspaceDetailView:
    workspace: WorkspaceProject
    files: list[WorkspaceFileView] = field(default_factory=list)


def _file_view(f: WorkspaceFile) -> WorkspaceFileView:
    return WorkspaceFileView(
        file_id=f.id,
        filename=f.filename,
        media_type=f.media_type,
        size=f.size,
        has_extracted_text=bool(f.extracted_text),
        created_at=f.created_at,
    )


class WorkspacesService:
    def __init__(self, repo: WorkspacesRepository, settings: Settings | None = None) -> None:
        self._repo = repo
        self._settings = settings or get_settings()

    # ---- workspace CRUD ----

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        name: str,
        description: str | None,
        instructions: str | None,
    ) -> WorkspaceProject:
        return await self._repo.create_workspace(
            user_id=user_id,
            name=name.strip(),
            description=description,
            instructions=instructions,
        )

    async def list_workspaces(
        self, *, user_id: uuid.UUID, cursor: str | None, limit: int
    ) -> WorkspaceListPage:
        decoded: WorkspaceCursor | None = None
        if cursor:
            try:
                decoded = WorkspaceCursor.decode(cursor)
            except InvalidCursorError as exc:
                raise ValidationFailedError("invalid cursor") from exc
        return await self._repo.list_workspaces(user_id=user_id, cursor=decoded, limit=limit)

    async def _require_workspace(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> WorkspaceProject:
        workspace = await self._repo.get_workspace(workspace_id, user_id)
        if workspace is None:
            raise NotFoundError("workspace not found")
        return workspace

    async def get_detail(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> WorkspaceDetailView:
        workspace = await self._require_workspace(workspace_id, user_id)
        files = await self._repo.list_files(workspace_id)
        return WorkspaceDetailView(workspace=workspace, files=[_file_view(f) for f in files])

    async def update(
        self,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        name: str | None,
        set_description: bool,
        description: str | None,
        set_instructions: bool,
        instructions: str | None,
    ) -> WorkspaceDetailView:
        workspace = await self._require_workspace(workspace_id, user_id)
        updated = await self._repo.update_workspace(
            workspace,
            name=name.strip() if name is not None else None,
            set_description=set_description,
            description=description,
            set_instructions=set_instructions,
            instructions=instructions,
        )
        files = await self._repo.list_files(workspace_id)
        return WorkspaceDetailView(workspace=updated, files=[_file_view(f) for f in files])

    async def delete(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
        deleted = await self._repo.delete_workspace(workspace_id, user_id)
        if not deleted:
            raise NotFoundError("workspace not found")

    # ---- files ----

    async def upload_file(
        self,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        req: WorkspaceFileUploadRequest,
    ) -> WorkspaceFileView:
        """Validate + persist one knowledge file (inline base64), extracting text at upload.

        Enforces the per-workspace count and total-bytes limits (ADR-036 §4) AFTER validating the
        single file. Owner isolation: a foreign/missing workspace → 404 before any write.
        """
        await self._require_workspace(workspace_id, user_id)

        # Per-workspace count cap (ADR-036 §4) → 422 when adding would exceed it.
        if await self._repo.file_count(workspace_id) >= self._settings.workspace_file_max_count:
            raise ValidationFailedError("workspace file count limit exceeded")

        extracted = validate_and_extract(req, self._settings)

        # Per-workspace total-bytes cap (ADR-036 §4) → 422 when adding would exceed it.
        current_total = await self._repo.total_bytes(workspace_id)
        if current_total + extracted.size > self._settings.workspace_files_total_bytes:
            raise ValidationFailedError("workspace total size limit exceeded")

        row = await self._repo.add_file(
            workspace_id=workspace_id,
            filename=req.filename,
            content=extracted.content,
            media_type=req.mediaType,
            size=extracted.size,
            extracted_text=extracted.extracted_text,
        )
        return _file_view(row)

    async def list_files(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[WorkspaceFileView]:
        await self._require_workspace(workspace_id, user_id)
        files = await self._repo.list_files(workspace_id)
        return [_file_view(f) for f in files]

    async def delete_file(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID, file_id: uuid.UUID
    ) -> None:
        await self._require_workspace(workspace_id, user_id)
        deleted = await self._repo.delete_file(workspace_id, file_id)
        if not deleted:
            raise NotFoundError("file not found")

    # ---- orchestrator helpers ----

    async def owns_workspace(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """True when the workspace exists and belongs to the user (ADR-036 §3 binding check)."""
        return await self._repo.get_workspace(workspace_id, user_id) is not None

    async def instructions_for_session(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> str | None:
        """Read ONLY the workspace instructions for a continuation turn (ADR-036 §3).

        Unlike ``context_for_session`` (turn 0, assembles instructions + knowledge files), this is a
        light single-column read used on EVERY ``/chat/tool-result`` continuation: knowledge files
        are already persisted as content blocks in the message history, but ``instructions`` live in
        the ``system`` param (NOT in history) and must be re-injected on each LLM call. Returns None
        when the workspace was deleted/foreign or its instructions are empty (→ base system prompt).
        """
        instructions = await self._repo.get_instructions(workspace_id, user_id)
        return instructions or None

    # ---- context assembly (orchestrator) ----

    async def context_for_session(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID, *, provider: str
    ) -> WorkspaceContext | None:
        """Assemble (instructions, files) context for a workspace chat's first turn (ADR-036 §3/§6).

        Returns None when the workspace no longer exists or is foreign (the session keeps working as
        a plain chat — defensive; the binding was validated at session creation). Otherwise returns
        the instructions to inject and a PreparedAttachments of the knowledge files:
        - document/text with non-empty extracted_text → a text block ``[Файл проекта: {filename}]``,
          truncated collectively to WORKSPACE_CONTEXT_MAX_CHARS (created_at ASC, tail-truncated);
        - image → a provider vision block (Anthropic image / OpenAI image_url data-URI).
        Images are NOT counted against the char limit (ADR-036 §6).
        """
        workspace = await self._repo.get_workspace(workspace_id, user_id)
        if workspace is None:
            return None
        files = await self._repo.list_files(workspace_id)
        attachments = self._build_file_attachments(files, provider)
        instructions = workspace.instructions or None
        if instructions is None and attachments is None:
            return WorkspaceContext(instructions=None, attachments=None)
        return WorkspaceContext(instructions=instructions, attachments=attachments)

    def _build_file_attachments(
        self, files: list[WorkspaceFile], provider: str
    ) -> PreparedAttachments | None:
        """Build provider content blocks for the knowledge files (ADR-036 §6).

        Text blocks (document/text) are budget-limited by WORKSPACE_CONTEXT_MAX_CHARS across all
        files in created_at order (oldest first); the file that crosses the budget is tail-truncated
        and later text files are dropped. Images are appended as vision blocks regardless of the
        char budget. Returns None when there is nothing to inject.
        """
        max_chars = self._settings.workspace_context_max_chars
        used = 0
        content_blocks: list[dict[str, object]] = []
        for f in files:
            if f.media_type in _IMAGE_TYPES:
                block = self._image_block(f, provider)
                if block is not None:
                    content_blocks.append(block)
                continue
            text = f.extracted_text
            if not text:
                continue
            if used >= max_chars:
                continue
            remaining = max_chars - used
            snippet = text[:remaining]
            used += len(snippet)
            label = f"[Файл проекта: {f.filename}]\n{snippet}"
            # SSOT for the Responses wire format of these blocks: attachments.py
            # `_openai_content_block` (input_text / input_image + flat image_url + detail). OpenAI
            # workspace blocks are injected VERBATIM by openai_client `_inject_attachments` (no
            # `{type:text}`→`input_text` normalization, unlike `_user_item_from_blocks`), so they
            # MUST already be Responses parts. Anthropic keeps `{type:text}` (ADR-059 §6).
            if provider == "openai":
                content_blocks.append({"type": "input_text", "text": label})
            else:
                content_blocks.append({"type": "text", "text": label})
        if not content_blocks:
            return None
        # placeholders are unused for workspace files (we never persist these as a user step — the
        # blocks are injected only into the live first-turn request), but PreparedAttachments
        # requires the field; keep it empty.
        return PreparedAttachments(content_blocks=content_blocks, placeholders=[])

    @staticmethod
    def _image_block(f: WorkspaceFile, provider: str) -> dict[str, object] | None:
        """Provider vision block for an image knowledge file (ADR-036 §6, ADR-033 §5).

        Anthropic: native image block with base64 source. OpenAI: image_url data-URI. The bytes are
        base64-encoded from the stored BYTEA content (re-encoded once per first turn).
        """
        data = base64.b64encode(f.content).decode("ascii")
        if provider == "openai":
            # SSOT for this Responses block shape: attachments.py `_openai_content_block`
            # (input_image + FLAT `image_url` data-URI + `detail`). Injected VERBATIM by
            # openai_client `_inject_attachments` — no normalization, so it must already be a
            # Responses part (ADR-059 §6).
            return {
                "type": "input_image",
                "image_url": f"data:{f.media_type};base64,{data}",
                "detail": "auto",
            }
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": f.media_type, "data": data},
        }
