"""Chats schemas for /v1/chats* (chats/02-api-contracts.md)."""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from pydantic import Field, model_validator

from app.schemas.common import StrictModel

_TITLE_MAX = 200


class ChatListItemSchema(StrictModel):
    id: uuid.UUID = Field(description="Идентификатор чата.")
    title: str | None = Field(default=None, description="Заголовок чата (или null).")
    preview: str | None = Field(
        default=None, description="Срез текста последнего сообщения (или null)."
    )
    assistantMode: Literal["chat", "code"] = Field(description="Тип ассистента (chat|code).")
    isPinned: bool = Field(description="Закреплён ли чат.")
    projectId: str | None = Field(
        default=None,
        description=(
            "Идентификатор website-builder-проекта: свободная строка, заданная при создании "
            "сессии. null = «чистый чат» (проект не активирован). Независим от workspaceProjectId."
        ),
    )
    workspaceProjectId: uuid.UUID | None = Field(
        default=None,
        description="Привязка к рабочему пространству (или null).",
    )
    updatedAt: datetime.datetime = Field(description="Время последнего обновления (ISO8601).")


class ChatListResponse(StrictModel):
    items: list[ChatListItemSchema] = Field(description="Список чатов на текущей странице.")
    nextCursor: str | None = Field(
        default=None, description="Курсор следующей страницы (или null, если страниц больше нет)."
    )


class ChatStepSchema(StrictModel):
    id: uuid.UUID = Field(description="Идентификатор шага.")
    messageStepId: uuid.UUID = Field(description="Идентификатор message-шага.")
    role: Literal["user", "assistant", "tool"] = Field(description="Роль шага.")
    payload: dict[str, Any] = Field(description="Content-блоки шага.")
    usage: dict[str, Any] | None = Field(
        default=None, description="Потребление токенов (для assistant-шагов)."
    )
    createdAt: datetime.datetime = Field(description="Время создания шага (ISO8601).")


class ChatHistoryResponse(StrictModel):
    id: uuid.UUID = Field(description="Идентификатор чата.")
    title: str | None = Field(default=None, description="Заголовок чата.")
    assistantMode: Literal["chat", "code"] = Field(description="Тип ассистента (chat|code).")
    mode: Literal["credits", "byok"] = Field(description="Режим оплаты сессии.")
    steps: list[ChatStepSchema] = Field(description="Упорядоченные шаги чата.")


class StepsViewStepSchema(StrictModel):
    kind: Literal["reasoning", "tool_call", "tool_result", "assistant_message"] = Field(
        description="Тип шага для UI."
    )
    toolName: str | None = Field(
        default=None, description="Доменное имя инструмента (с точкой) или null."
    )
    summary: str = Field(description="Краткое человекочитаемое описание шага.")
    createdAt: datetime.datetime = Field(description="Время шага (ISO8601).")


class StepsViewResponse(StrictModel):
    messageStepId: uuid.UUID = Field(description="Message-шаг, для которого построен steps-view.")
    stepCount: int = Field(description="Число шагов.")
    steps: list[StepsViewStepSchema] = Field(description="Плоский список шагов.")


class ChatPatchRequest(StrictModel):
    title: str | None = Field(
        default=None, max_length=_TITLE_MAX, description="Новый заголовок (≤ 200 символов)."
    )
    isPinned: bool | None = Field(default=None, description="Закрепить/открепить чат.")
    workspaceProjectId: uuid.UUID | None = Field(
        default=None,
        description=(
            "Управление привязкой чата к воркспейсу. Поле отсутствует → привязка не трогается; "
            "uuid → перенести/сменить (валидируется принадлежность пользователю, чужой → "
            "404 workspace_not_found); null → убрать из воркспейса."
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> ChatPatchRequest:
        # At least one field present (ADR-038: title/isPinned/workspaceProjectId — presence in
        # model_fields_set). absent vs explicit-null is distinguished via model_fields_set: an
        # explicit null for title/workspaceProjectId counts as a requested change (field present),
        # whereas a field absent from the body means "no change requested" for that field.
        has_title = "title" in self.model_fields_set
        has_workspace = "workspaceProjectId" in self.model_fields_set
        if not has_title and self.isPinned is None and not has_workspace:
            raise ValueError("at least one of title/isPinned/workspaceProjectId is required")
        return self


class ChatPatchResponse(StrictModel):
    id: uuid.UUID = Field(description="Идентификатор чата.")
    title: str | None = Field(default=None, description="Актуальный заголовок.")
    isPinned: bool = Field(description="Актуальное состояние закрепления.")
    workspaceProjectId: uuid.UUID | None = Field(
        default=None, description="Актуальная привязка к воркспейсу после изменения (или null)."
    )
    updatedAt: datetime.datetime = Field(description="Время обновления (ISO8601).")


class ChatDeleteResponse(StrictModel):
    deleted: bool = Field(description="Признак успешного удаления.")
