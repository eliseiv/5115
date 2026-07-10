"""Auth-issuer routes: /v1/auth/register|token|refresh|jwks (auth/02, ADR-018).

Public (no user JWT — this is where the token is obtained); throttled per source IP. Issuer
endpoints return 503 when no private signing key is configured. Tokens are never logged.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api_gateway.rate_limit import enforce_auth_limits
from app.auth.issuer import build_jwks
from app.auth.service import AuthService, IssuedTokens
from app.config import get_settings
from app.deps import client_ip, get_auth_service
from app.errors import NotFoundError, RateLimitedError
from app.schemas.auth import (
    AppleSignInRequest,
    JwksResponse,
    RefreshRequest,
    RegisterRequest,
    TokenRequest,
    TokenResponse,
)

# No bearer_scheme / get_current_user here: these endpoints are public (R2.3, ADR-018 §2).
router = APIRouter(prefix="/v1/auth", tags=["Auth"])


async def _rate_limit(request: Request) -> None:
    if not await enforce_auth_limits(ip=client_ip(request)):
        raise RateLimitedError("rate limit exceeded")


def _to_response(tokens: IssuedTokens) -> TokenResponse:
    return TokenResponse(
        userId=tokens.user_id,
        deviceId=tokens.device_id,
        accessToken=tokens.access_token,
        tokenType="Bearer",
        expiresIn=tokens.expires_in,
        refreshToken=tokens.refresh_token,
        refreshExpiresIn=tokens.refresh_expires_in,
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    summary="Регистрация устройства",
    description=(
        "Создаёт или находит идентичность устройства и выдаёт пару токенов. `deviceId` "
        "опционален — без него сервер сгенерирует и вернёт его. Известное устройство возвращает "
        "тот же `userId`. `503`, если выпуск токенов не настроен."
    ),
)
async def auth_register(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    body: RegisterRequest,
) -> TokenResponse:
    await _rate_limit(request)
    tokens = await auth.register_or_token(body.deviceId)
    return _to_response(tokens)


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Токены для устройства",
    description=(
        "Выдаёт пару токенов для уже известного устройства (тот же `userId`). `deviceId` "
        "обязателен. `503`, если выпуск токенов не настроен."
    ),
)
async def auth_token(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    body: TokenRequest,
) -> TokenResponse:
    await _rate_limit(request)
    tokens = await auth.register_or_token(body.deviceId)
    return _to_response(tokens)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Обновить токены",
    description=(
        "Обменивает refresh-токен на новую пару. Refresh-токен одноразовый: после обмена "
        "становится недействительным. Повторное использование, невалидный или истёкший "
        "токен — `401`."
    ),
)
async def auth_refresh(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    body: RefreshRequest,
) -> TokenResponse:
    await _rate_limit(request)
    tokens = await auth.refresh(body.refreshToken)
    return _to_response(tokens)


@router.post(
    "/apple",
    response_model=TokenResponse,
    summary="Вход через Apple",
    description=(
        "Принимает Apple identity token (нативный Sign in with Apple), верифицирует его и "
        "выдаёт нашу пару токенов — кросс-девайс аккаунт (один Apple-аккаунт = один `userId`). "
        "`deviceId` опционален. Невалидный/просроченный токен — `401`. `503`, если выпуск "
        "токенов не настроен или Apple-аудитория не задана."
    ),
)
async def auth_apple(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    body: AppleSignInRequest,
) -> TokenResponse:
    # "not configured" (503) and verification failures (401) are raised inside the service /
    # verifier and mapped by the global error handler (ServiceUnavailableError / UnauthorizedError).
    await _rate_limit(request)
    tokens = await auth.sign_in_with_apple(
        identity_token=body.identityToken, device_id=body.deviceId, nonce=body.nonce
    )
    return _to_response(tokens)


@router.get(
    "/jwks",
    response_model=JwksResponse,
    summary="Публичный ключ (JWKS)",
    description=(
        "Публичный ключ подписи в формате JWKS для самопроверки токенов. Приватный ключ не "
        "отдаётся. `404`, если JWKS отключён или публичный ключ не настроен."
    ),
)
async def auth_jwks(request: Request) -> JwksResponse:
    await _rate_limit(request)
    settings = get_settings()
    if not settings.auth_jwks_enabled:
        raise NotFoundError("jwks disabled")
    public_key = settings.resolve_public_key()
    if not public_key:
        raise NotFoundError("public key not configured")
    return JwksResponse.model_validate(build_jwks(public_key, settings.jwt_kid))
