"""Profile schemas for /v1/profile (profile/02-api-contracts.md)."""

from __future__ import annotations

import datetime

from pydantic import Field

from app.schemas.common import StrictModel

_DISPLAY_NAME_MAX = 80


class ProfileResponse(StrictModel):
    accountId: str = Field(
        description="Человекочитаемый идентификатор аккаунта (производная от userId, стабилен)."
    )
    displayName: str | None = Field(
        default=None, description="Отображаемое имя пользователя (или null, если не задано)."
    )
    createdAt: datetime.datetime = Field(description="Дата создания аккаунта (ISO8601).")


class ProfileUpdateRequest(StrictModel):
    displayName: str = Field(
        max_length=_DISPLAY_NAME_MAX,
        description=(
            "Новое отображаемое имя (≤ 80 символов). Пустая строка трактуется как сброс в null."
        ),
    )

    def normalized(self) -> str | None:
        """Empty string → reset to null (profile/02 default)."""
        stripped = self.displayName.strip()
        return stripped or None
