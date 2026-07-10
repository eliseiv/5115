"""Subscription route: POST /v1/subscription/sync (subscription/02)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_subscription_service, require_owner
from app.errors import RateLimitedError
from app.schemas.subscription import SubscriptionSyncRequest, SubscriptionSyncResponse
from app.subscription.service import SubscriptionService

router = APIRouter(prefix="/v1/subscription", tags=["Subscription"])


@router.post(
    "/sync",
    response_model=SubscriptionSyncResponse,
    summary="Синхронизировать подписку",
    description=(
        "Пришлите подписанную StoreKit-транзакцию в поле `transaction`. Обновляет состояние "
        "подписки; при активации или продлении начисляет кредиты периода."
    ),
)
async def subscription_sync(
    body: SubscriptionSyncRequest,
    request: Request,
    current: CurrentUser,
    subscription: Annotated[SubscriptionService, Depends(get_subscription_service)],
) -> SubscriptionSyncResponse:
    require_owner(body.userId, current)
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    result = await subscription.sync(current.user_id, body.transaction)
    return SubscriptionSyncResponse(
        isSubscribed=result.is_subscribed,
        expiresAt=result.expires_at,
        plan=result.plan,
    )
