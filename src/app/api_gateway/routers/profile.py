"""Profile routes: GET/PATCH /v1/profile (profile/02-api-contracts.md)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_profile_service
from app.errors import RateLimitedError
from app.profile.service import ProfileService, ProfileView
from app.schemas.profile import ProfileResponse, ProfileUpdateRequest

router = APIRouter(prefix="/v1/profile", tags=["Profile"])


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


def _to_response(view: ProfileView) -> ProfileResponse:
    return ProfileResponse(
        accountId=view.account_id,
        displayName=view.display_name,
        createdAt=view.created_at,
    )


@router.get(
    "",
    response_model=ProfileResponse,
    summary="Получить профиль",
    description=(
        "Возвращает профиль текущего пользователя: человекочитаемый `accountId` (производная "
        "от userId), `displayName` и дату создания. Данные строго скоупятся `sub`."
    ),
)
async def get_profile(
    request: Request,
    current: CurrentUser,
    profile: Annotated[ProfileService, Depends(get_profile_service)],
) -> ProfileResponse:
    await _rate_limit(current.user_id)
    return _to_response(await profile.get(current.user_id))


@router.patch(
    "",
    response_model=ProfileResponse,
    summary="Обновить профиль",
    description=(
        "Обновляет `displayName` (≤ 80 символов; пустая строка очищает имя в null). "
        "Возвращает обновлённый профиль."
    ),
)
async def patch_profile(
    body: ProfileUpdateRequest,
    request: Request,
    current: CurrentUser,
    profile: Annotated[ProfileService, Depends(get_profile_service)],
) -> ProfileResponse:
    await _rate_limit(current.user_id)
    view = await profile.update_display_name(current.user_id, body.normalized())
    return _to_response(view)
