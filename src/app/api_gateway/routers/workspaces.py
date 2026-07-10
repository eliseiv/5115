"""Workspaces routes: CRUD + knowledge files (workspaces/02-api-contracts.md, ADR-036).

JWT-protected (CurrentUser), owner-scoped; a foreign/missing workspace or file → 404 (the service
raises NotFoundError). Knowledge-file upload is inline base64 (reuses the attachment validation /
text extraction). Per-user rate limit like the other read/write endpoints.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_workspaces_service
from app.errors import RateLimitedError
from app.schemas.workspaces import (
    WorkspaceCreateRequest,
    WorkspaceCreateResponse,
    WorkspaceDeleteResponse,
    WorkspaceDetailResponse,
    WorkspaceFileMetaSchema,
    WorkspaceFilesListResponse,
    WorkspaceFileUploadRequest,
    WorkspaceListItemSchema,
    WorkspaceListResponse,
    WorkspacePatchRequest,
)
from app.workspaces.service import WorkspaceDetailView, WorkspaceFileView, WorkspacesService

router = APIRouter(prefix="/v1/workspaces", tags=["Workspaces"])

_LIST_LIMIT_DEFAULT = 50
_LIST_LIMIT_MAX = 100


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


def _file_meta(view: WorkspaceFileView) -> WorkspaceFileMetaSchema:
    return WorkspaceFileMetaSchema(
        fileId=view.file_id,
        filename=view.filename,
        mediaType=view.media_type,
        size=view.size,
        hasExtractedText=view.has_extracted_text,
        createdAt=view.created_at,
    )


def _detail_response(view: WorkspaceDetailView) -> WorkspaceDetailResponse:
    w = view.workspace
    return WorkspaceDetailResponse(
        id=w.id,
        name=w.name,
        description=w.description,
        instructions=w.instructions,
        files=[_file_meta(f) for f in view.files],
        createdAt=w.created_at,
        updatedAt=w.updated_at,
    )


@router.post(
    "",
    response_model=WorkspaceCreateResponse,
    status_code=201,
    summary="Создать рабочее пространство",
    description="Создаёт workspace (`name` обязателен; `description`/`instructions` опциональны).",
)
async def create_workspace(
    body: WorkspaceCreateRequest,
    request: Request,
    current: CurrentUser,
    workspaces: Annotated[WorkspacesService, Depends(get_workspaces_service)],
) -> WorkspaceCreateResponse:
    await _rate_limit(current.user_id)
    w = await workspaces.create(
        user_id=current.user_id,
        name=body.name,
        description=body.description,
        instructions=body.instructions,
    )
    return WorkspaceCreateResponse(
        id=w.id,
        name=w.name,
        description=w.description,
        instructions=w.instructions,
        createdAt=w.created_at,
        updatedAt=w.updated_at,
    )


@router.get(
    "",
    response_model=WorkspaceListResponse,
    summary="Список рабочих пространств",
    description=(
        "Список workspace пользователя по свежести (`updatedAt DESC`). Курсорная пагинация "
        "(`cursor`, `limit` 1..100, дефолт 50). Каждый элемент несёт `fileCount`/`chatCount`."
    ),
)
async def list_workspaces(
    request: Request,
    current: CurrentUser,
    workspaces: Annotated[WorkspacesService, Depends(get_workspaces_service)],
    cursor: Annotated[str | None, Query(description="Курсор пагинации (opaque).")] = None,
    limit: Annotated[
        int, Query(ge=1, le=_LIST_LIMIT_MAX, description="Размер страницы (1..100).")
    ] = _LIST_LIMIT_DEFAULT,
) -> WorkspaceListResponse:
    await _rate_limit(current.user_id)
    page = await workspaces.list_workspaces(user_id=current.user_id, cursor=cursor, limit=limit)
    return WorkspaceListResponse(
        items=[
            WorkspaceListItemSchema(
                id=item.workspace.id,
                name=item.workspace.name,
                description=item.workspace.description,
                updatedAt=item.workspace.updated_at,
                fileCount=item.file_count,
                chatCount=item.chat_count,
            )
            for item in page.items
        ],
        nextCursor=page.next_cursor,
    )


@router.get(
    "/{workspace_id}",
    response_model=WorkspaceDetailResponse,
    summary="Рабочее пространство",
    description="Полный объект workspace (включая `instructions` и список файлов-знаний).",
)
async def get_workspace(
    request: Request,
    current: CurrentUser,
    workspaces: Annotated[WorkspacesService, Depends(get_workspaces_service)],
    workspace_id: Annotated[uuid.UUID, Path(description="Идентификатор workspace.")],
) -> WorkspaceDetailResponse:
    await _rate_limit(current.user_id)
    view = await workspaces.get_detail(workspace_id, current.user_id)
    return _detail_response(view)


@router.patch(
    "/{workspace_id}",
    response_model=WorkspaceDetailResponse,
    summary="Обновить рабочее пространство",
    description=(
        "Обновление `name`/`description`/`instructions` (хотя бы одно поле). "
        "`description`/`instructions` можно очистить, передав `null`."
    ),
)
async def patch_workspace(
    body: WorkspacePatchRequest,
    request: Request,
    current: CurrentUser,
    workspaces: Annotated[WorkspacesService, Depends(get_workspaces_service)],
    workspace_id: Annotated[uuid.UUID, Path(description="Идентификатор workspace.")],
) -> WorkspaceDetailResponse:
    await _rate_limit(current.user_id)
    fields = body.model_fields_set
    view = await workspaces.update(
        workspace_id,
        current.user_id,
        name=body.name,
        set_description="description" in fields,
        description=body.description,
        set_instructions="instructions" in fields,
        instructions=body.instructions,
    )
    return _detail_response(view)


@router.delete(
    "/{workspace_id}",
    response_model=WorkspaceDeleteResponse,
    summary="Удалить рабочее пространство",
    description=(
        "Удаляет workspace: файлы-знания каскадно, чаты остаются как «чистые» "
        "(`workspace_project_id` → NULL). Повторное удаление → 404."
    ),
)
async def delete_workspace(
    request: Request,
    current: CurrentUser,
    workspaces: Annotated[WorkspacesService, Depends(get_workspaces_service)],
    workspace_id: Annotated[uuid.UUID, Path(description="Идентификатор workspace.")],
) -> WorkspaceDeleteResponse:
    await _rate_limit(current.user_id)
    await workspaces.delete(workspace_id, current.user_id)
    return WorkspaceDeleteResponse(deleted=True)


@router.post(
    "/{workspace_id}/files",
    response_model=WorkspaceFileMetaSchema,
    status_code=201,
    summary="Загрузить файл-знание",
    description=(
        "Загрузка файла-знания (inline base64). Backend извлекает текст (document/text) и хранит "
        "байты. Лимиты: ≤20 файлов, ≤8 MB/файл, ≤32 MB суммарно на workspace."
    ),
)
async def upload_file(
    body: WorkspaceFileUploadRequest,
    request: Request,
    current: CurrentUser,
    workspaces: Annotated[WorkspacesService, Depends(get_workspaces_service)],
    workspace_id: Annotated[uuid.UUID, Path(description="Идентификатор workspace.")],
) -> WorkspaceFileMetaSchema:
    await _rate_limit(current.user_id)
    view = await workspaces.upload_file(workspace_id, current.user_id, body)
    return _file_meta(view)


@router.get(
    "/{workspace_id}/files",
    response_model=WorkspaceFilesListResponse,
    summary="Файлы-знания workspace",
    description="Список файлов-знаний workspace (метаданные; тело файлов не отдаётся).",
)
async def list_files(
    request: Request,
    current: CurrentUser,
    workspaces: Annotated[WorkspacesService, Depends(get_workspaces_service)],
    workspace_id: Annotated[uuid.UUID, Path(description="Идентификатор workspace.")],
) -> WorkspaceFilesListResponse:
    await _rate_limit(current.user_id)
    files = await workspaces.list_files(workspace_id, current.user_id)
    return WorkspaceFilesListResponse(items=[_file_meta(f) for f in files])


@router.delete(
    "/{workspace_id}/files/{file_id}",
    response_model=WorkspaceDeleteResponse,
    summary="Удалить файл-знание",
    description="Удаляет файл-знание (вместе с байтами). Отсутствующий/чужой → 404.",
)
async def delete_file(
    request: Request,
    current: CurrentUser,
    workspaces: Annotated[WorkspacesService, Depends(get_workspaces_service)],
    workspace_id: Annotated[uuid.UUID, Path(description="Идентификатор workspace.")],
    file_id: Annotated[uuid.UUID, Path(description="Идентификатор файла-знания.")],
) -> WorkspaceDeleteResponse:
    await _rate_limit(current.user_id)
    await workspaces.delete_file(workspace_id, current.user_id, file_id)
    return WorkspaceDeleteResponse(deleted=True)
