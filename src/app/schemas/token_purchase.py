"""Token-purchase schemas for /v1/tokens/* (token-purchase/02-api-contracts.md)."""

from __future__ import annotations

import uuid

from pydantic import Field

from app.schemas.common import StrictModel


class TokenPurchaseRequest(StrictModel):
    userId: uuid.UUID = Field(
        description="Идентификатор пользователя. Обязан совпадать с `sub` JWT."
    )
    # Signed StoreKit consumable JWS transaction (compact JWS). Never logged (redaction).
    transaction: str = Field(
        min_length=1,
        description="Подписанная StoreKit-транзакция покупки.",
    )


class TokenPurchaseResponse(StrictModel):
    creditsAdded: int = Field(
        description=(
            "Сколько кредитов начислено этой покупкой. При повторной (уже обработанной) "
            "транзакции — `0` (идемпотентность)."
        )
    )
    newBalance: int = Field(description="Текущий баланс кредитов после покупки.")
    transactionId: str = Field(description="Идентификатор обработанной StoreKit-транзакции.")


class TokenProduct(StrictModel):
    productId: str = Field(description="StoreKit productId пакета токенов.")
    credits: int = Field(description="Сколько кредитов начисляется за этот пакет.")


class TokenProductsResponse(StrictModel):
    products: list[TokenProduct] = Field(
        description="Каталог пакетов токенов (productId → credits). Цены — из StoreKit на клиенте."
    )
