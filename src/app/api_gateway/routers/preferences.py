"""Preferences routes: GET/PATCH /v1/preferences (preferences/02-api-contracts.md)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_preferences_service
from app.errors import RateLimitedError
from app.preferences.service import PreferencesService
from app.schemas.preferences import PreferencesPatchRequest, PreferencesResponse

router = APIRouter(prefix="/v1/preferences", tags=["Preferences"])


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


@router.get(
    "",
    response_model=PreferencesResponse,
    summary="Получить настройки",
    description=(
        "Возвращает пользовательские настройки: дефолтный тип ассистента (chat|code), "
        "toggle уведомлений и дефолты Code-контекста. Если настройки ещё не заданы — "
        "возвращаются значения по умолчанию (chat / true / {})."
    ),
)
async def get_preferences(
    request: Request,
    current: CurrentUser,
    prefs: Annotated[PreferencesService, Depends(get_preferences_service)],
) -> PreferencesResponse:
    await _rate_limit(current.user_id)
    view = await prefs.get(current.user_id)
    return PreferencesResponse(
        defaultAssistantMode=view.default_assistant_mode,
        defaultDialogMode=view.default_dialog_mode,
        notificationsEnabled=view.notifications_enabled,
        codeDefaults=view.code_defaults,
    )


@router.patch(
    "",
    response_model=PreferencesResponse,
    summary="Обновить настройки",
    description=(
        "Частично обновляет настройки (любое подмножество полей). Создаёт строку при "
        "отсутствии (upsert). Возвращает полный актуальный объект настроек."
    ),
)
async def patch_preferences(
    body: PreferencesPatchRequest,
    request: Request,
    current: CurrentUser,
    prefs: Annotated[PreferencesService, Depends(get_preferences_service)],
) -> PreferencesResponse:
    await _rate_limit(current.user_id)
    view = await prefs.patch(
        current.user_id,
        default_assistant_mode=body.defaultAssistantMode,
        default_dialog_mode=body.defaultDialogMode,
        notifications_enabled=body.notificationsEnabled,
        code_defaults=body.codeDefaults,
    )
    return PreferencesResponse(
        defaultAssistantMode=view.default_assistant_mode,
        defaultDialogMode=view.default_dialog_mode,
        notificationsEnabled=view.notifications_enabled,
        codeDefaults=view.code_defaults,
    )
