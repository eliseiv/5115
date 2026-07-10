"""Subscription schemas for /v1/subscription/sync (subscription/02)."""

from __future__ import annotations

import datetime
import uuid

from pydantic import Field

from app.schemas.common import StrictModel


class SubscriptionSyncRequest(StrictModel):
    userId: uuid.UUID = Field(
        description="Идентификатор пользователя. Обязан совпадать с `sub` JWT."
    )
    # Signed StoreKit JWS transaction (compact JWS string). Never logged (redaction).
    transaction: str = Field(
        min_length=1,
        description="Подписанная StoreKit-транзакция.",
    )


class SubscriptionSyncResponse(StrictModel):
    isSubscribed: bool = Field(description="Активна ли подписка после синхронизации.")
    expiresAt: datetime.datetime | None = Field(
        default=None, description="Дата/время окончания текущего периода подписки (UTC), если есть."
    )
    plan: str | None = Field(
        default=None, description="Идентификатор тарифного плана подписки, если известен."
    )
