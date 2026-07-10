"""Adapty subscription webhook route: POST /v1/billing/adapty/webhook (ADR-029).

Called by Adapty (server-to-server), NOT by the iOS client. Static bearer auth via the per-route
``require_adapty_webhook`` dependency (isolated from the user JWT / admin token). The body is read
RAW (``await request.body()``) with NO Pydantic body model: a malformed verification ping must
yield 2xx, never 422 (which Adapty retries forever). Every authorized outcome is HTTP 200 except a
real internal failure, which propagates to a 500 (Adapty retries -> clean reprocessing).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.billing_adapty.auth import require_adapty_webhook
from app.billing_adapty.service import AdaptyWebhookService
from app.deps import get_adapty_webhook_service
from app.schemas.billing_adapty import AdaptyWebhookResponse

router = APIRouter(prefix="/v1/billing/adapty", tags=["Billing (Adapty)"])


@router.post(
    "/webhook",
    response_model=AdaptyWebhookResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_adapty_webhook)],
    summary="Вебхук подписок Adapty",
    description=(
        "Серверный вебхук Adapty (вызывает Adapty, не клиент). Авторизация — статический "
        "`Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>` (constant-time). Тело читается сырым, "
        "без Pydantic-валидации. После авторизации ответ всегда `200` со статусом "
        "`ignored | duplicate | applied`; `500` — только при незаданном секрете или реальном "
        "сбое БД (тогда Adapty ретраит). Идемпотентность по `event_id` + ledger idempotency-key."
    ),
)
async def adapty_webhook(
    request: Request,
    service: Annotated[AdaptyWebhookService, Depends(get_adapty_webhook_service)],
) -> AdaptyWebhookResponse:
    raw = await request.body()
    outcome = await service.handle(raw)
    return AdaptyWebhookResponse(
        result=outcome.result,
        reason=outcome.reason,
        event_type=outcome.event_type,
    )
