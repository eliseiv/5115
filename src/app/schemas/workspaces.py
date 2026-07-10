"""Workspaces schemas for /v1/workspaces* (workspaces/02-api-contracts.md, ADR-036).

JWT-protected, owner-scoped; all models forbid extra fields (StrictModel). The API never returns
file ``content``/``extractedText`` — only metadata (size/mediaType/hasExtractedText). Length limits
match 02-api-contracts.md: name ≤ 120, description ≤ 1000, instructions ≤ 16000.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Literal

from pydantic import Field, model_validator

from app.schemas.chat import AttachmentMediaType
from app.schemas.common import StrictModel

_NAME_MAX = 120
_DESCRIPTION_MAX = 1000
_INSTRUCTIONS_MAX = 16000


class WorkspaceCreateRequest(StrictModel):
    name: str = Field(
        min_length=1,
        max_length=_NAME_MAX,
        description="Название рабочего пространства (≤ 120 символов, непустое после strip).",
    )
    description: str | None = Field(
        default=None,
        max_length=_DESCRIPTION_MAX,
        description="Описание (≤ 1000 символов) или null.",
    )
    instructions: str | None = Field(
        default=None,
        max_length=_INSTRUCTIONS_MAX,
        description=(
            "Кастомный system-prompt проекта (≤ 16000 символов) или null. Подмешивается в "
            "system-prompt после base assistant_mode prompt."
        ),
    )

    @model_validator(mode="after")
    def _check_name(self) -> WorkspaceCreateRequest:
        if not self.name.strip():
            raise ValueError("name must be a non-empty string")
        return self


class WorkspacePatchRequest(StrictModel):
    name: str | None = Field(
        default=None,
        max_length=_NAME_MAX,
        description="Новое название (≤ 120 символов, непустое после strip).",
    )
    description: str | None = Field(
        default=None,
        max_length=_DESCRIPTION_MAX,
        description="Новое описание (≤ 1000) или null (очистить).",
    )
    instructions: str | None = Field(
        default=None,
        max_length=_INSTRUCTIONS_MAX,
        description="Новые инструкции (≤ 16000) или null (очистить).",
    )

    @model_validator(mode="after")
    def _check(self) -> WorkspacePatchRequest:
        # At least one field must be present (workspaces/02-api-contracts.md).
        fields = self.model_fields_set
        if not fields & {"name", "description", "instructions"}:
            raise ValueError("at least one of name/description/instructions is required")
        # name, when present, must be a non-empty string (cannot be cleared to null).
        if "name" in fields and (self.name is None or not self.name.strip()):
            raise ValueError("name must be a non-empty string when provided")
        return self


class WorkspaceFileMetaSchema(StrictModel):
    fileId: uuid.UUID = Field(description="Идентификатор файла-знания.")
    filename: str = Field(description="Имя файла.")
    mediaType: str = Field(description="MIME-тип файла.")
    size: int = Field(description="Размер файла в байтах (декодированный).")
    hasExtractedText: bool = Field(
        description="Извлечён ли текст (document/text → true; image → false)."
    )
    createdAt: datetime.datetime = Field(description="Время загрузки (ISO8601).")


class WorkspaceCreateResponse(StrictModel):
    id: uuid.UUID = Field(description="Идентификатор рабочего пространства.")
    name: str = Field(description="Название.")
    description: str | None = Field(default=None, description="Описание или null.")
    instructions: str | None = Field(default=None, description="Инструкции или null.")
    createdAt: datetime.datetime = Field(description="Время создания (ISO8601).")
    updatedAt: datetime.datetime = Field(description="Время обновления (ISO8601).")


class WorkspaceDetailResponse(StrictModel):
    id: uuid.UUID = Field(description="Идентификатор рабочего пространства.")
    name: str = Field(description="Название.")
    description: str | None = Field(default=None, description="Описание или null.")
    instructions: str | None = Field(default=None, description="Инструкции или null.")
    files: list[WorkspaceFileMetaSchema] = Field(
        description="Метаданные файлов-знаний (без содержимого/текста — тело наружу не отдаётся)."
    )
    createdAt: datetime.datetime = Field(description="Время создания (ISO8601).")
    updatedAt: datetime.datetime = Field(description="Время обновления (ISO8601).")


class WorkspaceListItemSchema(StrictModel):
    id: uuid.UUID = Field(description="Идентификатор рабочего пространства.")
    name: str = Field(description="Название.")
    description: str | None = Field(default=None, description="Описание или null.")
    updatedAt: datetime.datetime = Field(description="Время обновления (ISO8601).")
    fileCount: int = Field(description="Число файлов-знаний.")
    chatCount: int = Field(description="Число чатов проекта.")


class WorkspaceListResponse(StrictModel):
    items: list[WorkspaceListItemSchema] = Field(
        description="Список рабочих пространств на текущей странице."
    )
    nextCursor: str | None = Field(
        default=None, description="Курсор следующей страницы (или null, если страниц больше нет)."
    )


class WorkspaceFileUploadRequest(StrictModel):
    """Загрузка файла-знания (inline base64). Поля/валидации как у chat-вложений."""

    type: Literal["image", "document", "text"] = Field(
        description="Класс файла: `image` (фото), `document` (PDF) или `text` (текстовый файл)."
    )
    mediaType: AttachmentMediaType = Field(
        description=(
            "MIME-тип из allowlist: `image/jpeg|png|gif|webp`, `application/pdf`, "
            "`text/plain|markdown|csv`, `application/json`. Вне списка → 422."
        )
    )
    filename: str = Field(
        min_length=1,
        max_length=512,
        description="Имя файла (обязательно; используется в разметке контекста проекта).",
    )
    data: str = Field(
        min_length=1,
        description="Содержимое файла в base64. Только inline base64 — URL запрещены.",
    )

    @model_validator(mode="after")
    def _check_filename(self) -> WorkspaceFileUploadRequest:
        if not self.filename.strip():
            raise ValueError("filename must be a non-empty string")
        return self


class WorkspaceFilesListResponse(StrictModel):
    items: list[WorkspaceFileMetaSchema] = Field(
        description="Метаданные файлов-знаний workspace (без содержимого/текста)."
    )


class WorkspaceDeleteResponse(StrictModel):
    deleted: bool = Field(description="Признак успешного удаления.")
