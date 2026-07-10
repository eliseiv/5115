"""Schemas for the RU payment endpoints (billing-cloudpayments/02-api-contracts.md).

The webhook (ADR-050) reads the RAW request body (no Pydantic body model) so it has only a response
model documenting the ``{"code": 0}`` success envelope. The checkout endpoint (ADR-051) has a strict
request body (``CloudPaymentsCheckoutRequest``) and a passthrough response of the created payment
link (``CloudPaymentsCheckoutResponse``).
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import StrictModel


class CloudPaymentsWebhookResponse(BaseModel):
    code: int = Field(
        default=0,
        description="Код приёма вебхука. `0` — событие принято (платёж обработан или пропущен).",
        examples=[0],
    )


class CloudPaymentsCheckoutRequest(StrictModel):
    """Тело запроса на создание ссылки RU-оплаты. `userId` берётся из JWT, не из тела."""

    productId: str = Field(
        min_length=1,
        description="Код продукта (подписка или пакет токенов). Неизвестный/некредитуемый — `422`.",
        examples=["week_6.99_nottrial"],
    )
    customerEmail: EmailStr = Field(
        description="Email покупателя для платёжной формы.",
        examples=["user@example.com"],
    )


class CloudPaymentsCheckoutResponse(StrictModel):
    """Созданная платёжная ссылка. Клиент открывает `paymentUrl` для оплаты."""

    paymentId: str = Field(
        description="Идентификатор платежа.",
        examples=["e3d7ffe4-0000-0000-0000-000000000000"],
    )
    paymentUrl: str = Field(
        description="Ссылка на оплату — открыть в браузере/веб-вью.",
        examples=["https://yoomoney.ru/checkout/payments/v2/contract?orderId=..."],
    )
    status: str = Field(
        description="Статус платежа на момент создания ссылки.",
        examples=["pending"],
    )
    expiresAt: str | None = Field(
        default=None,
        description="Момент истечения ссылки (если задан провайдером), иначе `null`.",
        examples=[None],
    )
