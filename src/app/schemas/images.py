"""Images schemas for GET /v1/images (api-gateway/02-api-contracts.md §8)."""

from __future__ import annotations

import datetime
import uuid

from pydantic import Field

from app.schemas.common import StrictModel


class ImageListItem(StrictModel):
    imageId: uuid.UUID = Field(description="Идентификатор изображения.")
    contentType: str = Field(description="MIME-тип изображения.")
    size: int = Field(description="Размер изображения в байтах.")
    createdAt: datetime.datetime = Field(description="Время создания (ISO8601).")
    sessionId: uuid.UUID | None = Field(
        default=None,
        description="Идентификатор чата-источника (null для изображения временного чата).",
    )


class ImageListResponse(StrictModel):
    items: list[ImageListItem] = Field(description="Список изображений на текущей странице.")
    nextCursor: str | None = Field(
        default=None, description="Курсор следующей страницы (или null, если страниц больше нет)."
    )
