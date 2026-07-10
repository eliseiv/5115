"""Preferences schemas for /v1/preferences (preferences/02-api-contracts.md)."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, model_validator

from app.observability.redaction import _is_sensitive_key
from app.schemas.common import StrictModel

_CODE_DEFAULTS_MAX_BYTES = 8 * 1024


def _has_sensitive_keys(value: Any) -> bool:
    """Reject obvious secrets in codeDefaults (preferences/03: no secrets)."""
    if isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(key, str) and _is_sensitive_key(key):
                return True
            if _has_sensitive_keys(sub):
                return True
    elif isinstance(value, list | tuple):
        return any(_has_sensitive_keys(item) for item in value)
    return False


DialogMode = Literal["smart", "deep_thinking", "study_learn", "search"]


class PreferencesResponse(StrictModel):
    defaultAssistantMode: Literal["chat", "code"] = Field(
        description="Дефолтный тип ассистента (chat|code). Ортогонален режиму оплаты."
    )
    defaultDialogMode: DialogMode = Field(
        description=(
            "Дефолтный режим диалога (smart|deep_thinking|study_learn|search). "
            "Используется при создании сессии, если `dialogMode` не передан в /chat/run. "
            "Ортогонален `defaultAssistantMode` и режиму оплаты."
        )
    )
    notificationsEnabled: bool = Field(description="Включены ли уведомления (toggle).")
    codeDefaults: dict[str, Any] = Field(
        description="Дефолты Code-контекста (язык и т.п.). Без секретов."
    )


class PreferencesPatchRequest(StrictModel):
    defaultAssistantMode: Literal["chat", "code"] | None = Field(
        default=None, description="Новый дефолтный тип ассистента (chat|code)."
    )
    defaultDialogMode: DialogMode | None = Field(
        default=None,
        description=(
            "Новый дефолтный режим диалога (smart|deep_thinking|study_learn|search). "
            "Валидируется только членство в enum — сохранить любой режим допустимо на любом "
            "инстансе (provider-gate срабатывает при создании сессии в /chat/run, не здесь)."
        ),
    )
    notificationsEnabled: bool | None = Field(
        default=None, description="Новое значение toggle уведомлений."
    )
    codeDefaults: dict[str, Any] | None = Field(
        default=None,
        description="Новые дефолты Code-контекста (≤ 8KB сериализованного JSON, без секретов).",
    )

    @model_validator(mode="after")
    def _check(self) -> PreferencesPatchRequest:
        if (
            self.defaultAssistantMode is None
            and self.defaultDialogMode is None
            and self.notificationsEnabled is None
            and self.codeDefaults is None
        ):
            raise ValueError("at least one field is required")
        if self.codeDefaults is not None:
            if len(json.dumps(self.codeDefaults).encode("utf-8")) > _CODE_DEFAULTS_MAX_BYTES:
                raise ValueError("codeDefaults exceeds size limit")
            if _has_sensitive_keys(self.codeDefaults):
                raise ValueError("codeDefaults must not contain secrets")
        return self
