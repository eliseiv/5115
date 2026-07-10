"""BYOK schemas for /v1/byok/set|toggle|delete (byok/02)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field, field_validator

from app.config import get_settings
from app.schemas.common import StrictModel


class BYOKSetRequest(StrictModel):
    userId: uuid.UUID = Field(
        description="Идентификатор пользователя. Обязан совпадать с `sub` JWT."
    )
    apiKey: str = Field(
        min_length=1,
        description="Ключ Anthropic пользователя. Хранится зашифрованным. Не логируется.",
    )

    @field_validator("apiKey")
    @classmethod
    def _check_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > get_settings().size_limit_api_key:
            raise ValueError("apiKey exceeds size limit")
        return value


class BYOKToggleRequest(StrictModel):
    userId: uuid.UUID = Field(
        description="Идентификатор пользователя. Обязан совпадать с `sub` JWT."
    )
    enabled: bool = Field(
        description="Включить (`true`) или выключить (`false`) использование BYOK."
    )


class BYOKDeleteRequest(StrictModel):
    userId: uuid.UUID = Field(
        description="Идентификатор пользователя. Обязан совпадать с `sub` JWT."
    )


class BYOKResponse(StrictModel):
    byokEnabled: bool = Field(description="Включён ли режим BYOK для пользователя.")
    keyStatus: Literal["valid", "invalid", "missing", "validating", "offline", "expired"] = Field(
        description=(
            "Статус ключа: `missing` (не задан), `validating` (проверяется), `valid` "
            "(рабочий), `invalid` (401), `offline` (сетевая ошибка валидации), `expired` "
            "(был valid, отозван/истёк)."
        ),
    )
    activeModel: str | None = Field(
        default=None,
        description=(
            "Активная модель при `keyStatus=valid` (например `claude-sonnet-4-6`), иначе `null`."
        ),
    )
