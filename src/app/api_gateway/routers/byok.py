"""BYOK routes: /v1/byok/set|toggle|delete (byok/02)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.byok.service import BYOKService
from app.deps import CurrentUser, get_byok_service, require_owner
from app.errors import RateLimitedError
from app.schemas.byok import (
    BYOKDeleteRequest,
    BYOKResponse,
    BYOKSetRequest,
    BYOKToggleRequest,
)

router = APIRouter(prefix="/v1/byok", tags=["BYOK"])

_SET_REQUEST_EXAMPLES = {
    "set_key": {
        "summary": "Сохранить ключ Anthropic",
        "description": (
            "Поле `apiKey` в примере — плейсхолдер; передайте реальный ключ. Не логируется."
        ),
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "apiKey": "sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx",
        },
    },
}

_SET_RESPONSE_EXAMPLES = {
    "valid": {
        "summary": "Ключ валиден",
        "value": {"byokEnabled": True, "keyStatus": "valid"},
    },
    "invalid": {
        "summary": "Ключ невалиден",
        "value": {"byokEnabled": True, "keyStatus": "invalid"},
    },
}


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


@router.post(
    "/set",
    response_model=BYOKResponse,
    summary="Сохранить ключ BYOK",
    description=(
        "Сохраняет собственный ключ Anthropic (зашифрованным) и проверяет его валидность. "
        "Возвращает `keyStatus` (`valid`/`invalid`)."
    ),
    responses={200: {"content": {"application/json": {"examples": _SET_RESPONSE_EXAMPLES}}}},
)
async def byok_set(
    request: Request,
    current: CurrentUser,
    byok: Annotated[BYOKService, Depends(get_byok_service)],
    body: Annotated[BYOKSetRequest, Body(openapi_examples=_SET_REQUEST_EXAMPLES)],
) -> BYOKResponse:
    require_owner(body.userId, current)
    await _rate_limit(current.user_id)
    result = await byok.set_key(current.user_id, body.apiKey)
    return BYOKResponse(
        byokEnabled=result.byok_enabled,
        keyStatus=result.key_status,
        activeModel=result.active_model,
    )


@router.post(
    "/toggle",
    response_model=BYOKResponse,
    summary="Включить/выключить BYOK",
    description=(
        "Включает или выключает использование собственного ключа Anthropic (без удаления ключа)."
    ),
)
async def byok_toggle(
    body: BYOKToggleRequest,
    request: Request,
    current: CurrentUser,
    byok: Annotated[BYOKService, Depends(get_byok_service)],
) -> BYOKResponse:
    require_owner(body.userId, current)
    await _rate_limit(current.user_id)
    result = await byok.toggle(current.user_id, body.enabled)
    return BYOKResponse(
        byokEnabled=result.byok_enabled,
        keyStatus=result.key_status,
        activeModel=result.active_model,
    )


@router.post(
    "/delete",
    response_model=BYOKResponse,
    summary="Удалить ключ BYOK",
    description="Удаляет сохранённый ключ Anthropic пользователя и выключает BYOK.",
)
async def byok_delete(
    body: BYOKDeleteRequest,
    request: Request,
    current: CurrentUser,
    byok: Annotated[BYOKService, Depends(get_byok_service)],
) -> BYOKResponse:
    require_owner(body.userId, current)
    await _rate_limit(current.user_id)
    result = await byok.delete_key(current.user_id)
    return BYOKResponse(
        byokEnabled=result.byok_enabled,
        keyStatus=result.key_status,
        activeModel=result.active_model,
    )
