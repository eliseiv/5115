"""Chats routes: list/history/steps-view/rename-pin/delete (chats/02-api-contracts.md)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.chats.service import ChatsService
from app.deps import CurrentUser, get_chats_service
from app.errors import RateLimitedError
from app.schemas.chats import (
    ChatDeleteResponse,
    ChatHistoryResponse,
    ChatListItemSchema,
    ChatListResponse,
    ChatPatchRequest,
    ChatPatchResponse,
    ChatStepSchema,
    StepsViewResponse,
    StepsViewStepSchema,
)

router = APIRouter(prefix="/v1/chats", tags=["Chats"])

_LIST_LIMIT_DEFAULT = 30
_LIST_LIMIT_MAX = 100


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


@router.get(
    "",
    response_model=ChatListResponse,
    summary="Список чатов",
    description=(
        "Список чатов пользователя: закреплённые сверху, затем по свежести. Поддерживает поиск "
        "`q` (по заголовку и тексту первого сообщения) и курсорную пагинацию."
    ),
)
async def list_chats(
    request: Request,
    current: CurrentUser,
    chats: Annotated[ChatsService, Depends(get_chats_service)],
    q: Annotated[str | None, Query(description="Поиск по заголовку/тексту.")] = None,
    cursor: Annotated[str | None, Query(description="Курсор пагинации (opaque).")] = None,
    limit: Annotated[
        int, Query(ge=1, le=_LIST_LIMIT_MAX, description="Размер страницы (1..100).")
    ] = _LIST_LIMIT_DEFAULT,
    workspaceProjectId: Annotated[
        uuid.UUID | None,
        Query(description="Фильтр «чаты проекта»: только чаты с этим workspaceProjectId."),
    ] = None,
) -> ChatListResponse:
    await _rate_limit(current.user_id)
    view = await chats.list_chats(
        user_id=current.user_id,
        query=q,
        cursor=cursor,
        limit=limit,
        workspace_project_id=workspaceProjectId,
    )
    return ChatListResponse(
        items=[
            ChatListItemSchema(
                id=item.id,
                title=item.title,
                preview=item.preview,
                assistantMode=item.assistant_mode,
                isPinned=item.is_pinned,
                projectId=item.project_id,
                workspaceProjectId=item.workspace_project_id,
                updatedAt=item.updated_at,
            )
            for item in view.items
        ],
        nextCursor=view.next_cursor,
    )


@router.get(
    "/{chat_id}",
    response_model=ChatHistoryResponse,
    summary="История чата",
    description="История шагов чата (упорядочены по времени). Чужой/несуществующий чат → 404.",
)
async def get_chat(
    request: Request,
    current: CurrentUser,
    chats: Annotated[ChatsService, Depends(get_chats_service)],
    chat_id: Annotated[uuid.UUID, Path(description="Идентификатор чата.")],
) -> ChatHistoryResponse:
    await _rate_limit(current.user_id)
    view = await chats.get_history(chat_id, current.user_id)
    return ChatHistoryResponse(
        id=view.id,
        title=view.title,
        assistantMode=view.assistant_mode,
        mode=view.mode,
        steps=[
            ChatStepSchema(
                id=step.id,
                messageStepId=step.message_step_id,
                role=step.role,
                payload=step.payload,
                usage=step.usage,
                createdAt=step.created_at,
            )
            for step in view.steps
        ],
    )


@router.get(
    "/{chat_id}/steps",
    response_model=StepsViewResponse,
    summary="Steps-view чата",
    description=(
        "Агрегированные шаги message-шага (tool-calls/reasoning) для UI. По умолчанию — "
        "последний message-шаг."
    ),
)
async def get_chat_steps(
    request: Request,
    current: CurrentUser,
    chats: Annotated[ChatsService, Depends(get_chats_service)],
    chat_id: Annotated[uuid.UUID, Path(description="Идентификатор чата.")],
    messageStepId: Annotated[
        uuid.UUID | None, Query(description="Конкретный message-шаг (по умолчанию последний).")
    ] = None,
) -> StepsViewResponse:
    await _rate_limit(current.user_id)
    view = await chats.steps_view(chat_id, current.user_id, messageStepId)
    return StepsViewResponse(
        messageStepId=view.message_step_id,
        stepCount=view.step_count,
        steps=[
            StepsViewStepSchema(
                kind=step.kind,
                toolName=step.tool_name,
                summary=step.summary,
                createdAt=step.created_at,
            )
            for step in view.steps
        ],
    )


@router.patch(
    "/{chat_id}",
    response_model=ChatPatchResponse,
    summary="Переименовать/закрепить/перенести чат",
    description=(
        "Переименование (`title`), закрепление (`isPinned`) и/или перенос чата в воркспейс "
        "(`workspaceProjectId`: uuid — перенести/сменить, null — убрать привязку; отсутствие "
        "поля — не менять). Хотя бы одно поле. Чужой/несуществующий целевой workspace → "
        "404 workspace_not_found."
    ),
)
async def patch_chat(
    body: ChatPatchRequest,
    request: Request,
    current: CurrentUser,
    chats: Annotated[ChatsService, Depends(get_chats_service)],
    chat_id: Annotated[uuid.UUID, Path(description="Идентификатор чата.")],
) -> ChatPatchResponse:
    await _rate_limit(current.user_id)
    set_title = "title" in body.model_fields_set
    # ADR-038: distinguish absent vs explicit-null via model_fields_set (as for title). Field
    # absent → binding untouched; uuid → re-bind (validated); null → unbind.
    set_workspace = "workspaceProjectId" in body.model_fields_set
    session = await chats.rename_or_pin(
        chat_id,
        current.user_id,
        title=body.title,
        set_title=set_title,
        is_pinned=body.isPinned,
        set_workspace_project_id=set_workspace,
        workspace_project_id=body.workspaceProjectId,
    )
    return ChatPatchResponse(
        id=session.id,
        title=session.title,
        isPinned=session.is_pinned,
        workspaceProjectId=session.workspace_project_id,
        updatedAt=session.updated_at,
    )


@router.delete(
    "/{chat_id}",
    response_model=ChatDeleteResponse,
    summary="Удалить чат",
    description=(
        "Удаляет чат (каскадно — шаги и tool-calls). Повторное удаление уже удалённого → 404."
    ),
)
async def delete_chat(
    request: Request,
    current: CurrentUser,
    chats: Annotated[ChatsService, Depends(get_chats_service)],
    chat_id: Annotated[uuid.UUID, Path(description="Идентификатор чата.")],
) -> ChatDeleteResponse:
    await _rate_limit(current.user_id)
    await chats.delete_chat(chat_id, current.user_id)
    return ChatDeleteResponse(deleted=True)
