"""Auth-issuer schemas for /v1/auth/* (auth/02-api-contracts.md)."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.config import get_settings
from app.schemas.common import StrictModel

# deviceId: 1..128 chars, charset [A-Za-z0-9._:-] (anti-injection, auth/05).
DeviceId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class RegisterRequest(StrictModel):
    deviceId: DeviceId | None = Field(
        default=None,
        description="Идентификатор устройства. Если не передан — сервер сгенерирует и вернёт его.",
    )


class TokenRequest(StrictModel):
    deviceId: DeviceId = Field(
        description="Идентификатор устройства (обязателен). Известное устройство — тот же userId."
    )


class RefreshRequest(StrictModel):
    refreshToken: str = Field(
        min_length=1,
        description="Refresh-токен из прошлого ответа. Одноразовый: при обмене заменяется новым.",
    )


class AppleSignInRequest(StrictModel):
    """Sign in with Apple request (ADR-043, auth/02). identityToken/nonce are never logged."""

    identityToken: str = Field(
        min_length=1,
        description=(
            "Apple identity token (OIDC JWT, нативный Sign in with Apple). Обязателен. "
            "Никогда не логируется."
        ),
    )
    deviceId: DeviceId | None = Field(
        default=None,
        description="Идентификатор устройства. Если не передан — сервер сгенерирует и вернёт его.",
    )
    nonce: str | None = Field(
        default=None,
        description=(
            "Raw-nonce, переданный клиентом Apple. Проверяется, если в токене есть claim `nonce`. "
            "Никогда не логируется."
        ),
    )

    @field_validator("identityToken")
    @classmethod
    def _check_token_size(cls, value: str) -> str:
        # Size-bounded like apiKey (BYOK): cap the body before verification (anti-abuse).
        if len(value.encode("utf-8")) > get_settings().size_limit_api_key:
            raise ValueError("identityToken exceeds size limit")
        return value


class TokenResponse(StrictModel):
    userId: uuid.UUID = Field(description="Идентификатор пользователя (claim `sub` в JWT).")
    deviceId: str = Field(description="Идентификатор устройства, связанный с этим пользователем.")
    accessToken: str = Field(description="JWT доступа (RS256). Передавайте как `Bearer <token>`.")
    tokenType: Literal["Bearer"] = Field(description="Тип токена. Всегда `Bearer`.")
    expiresIn: int = Field(description="Время жизни access-токена в секундах.")
    refreshToken: str = Field(description="Refresh-токен для получения новой пары. Одноразовый.")
    refreshExpiresIn: int = Field(description="Время жизни refresh-токена в секундах.")


class JwksKey(StrictModel):
    kty: str = Field(description="Тип ключа. Для RS256 — `RSA`.")
    use: str = Field(description="Назначение ключа. `sig` (подпись).")
    alg: str = Field(description="Алгоритм. `RS256`.")
    kid: str = Field(description="Идентификатор ключа.")
    n: str = Field(description="Модуль RSA (base64url).")
    e: str = Field(description="Открытая экспонента RSA (base64url).")


class JwksResponse(StrictModel):
    keys: list[JwksKey] = Field(description="Набор публичных ключей подписи.")
