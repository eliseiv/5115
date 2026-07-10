"""Wallet schemas for /v1/wallet and /v1/wallet/consume (wallet-ledger/02)."""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import StrictModel


class LedgerTxView(StrictModel):
    id: uuid.UUID = Field(description="Идентификатор транзакции реестра.")
    type: Literal["credit", "debit"] = Field(
        description="Тип: `credit` (начисление) или `debit` (списание)."
    )
    amount: int = Field(description="Сумма транзакции в кредитах.")
    createdAt: datetime.datetime = Field(description="Время создания транзакции (UTC).")
    meta: dict[str, Any] = Field(description="Произвольные метаданные транзакции.")


class WalletResponse(StrictModel):
    balance: int = Field(description="Текущий баланс кредитов.")
    lastTransactions: list[LedgerTxView] = Field(
        description="Последние транзакции реестра (новые первыми)."
    )


class WalletConsumeRequest(StrictModel):
    userId: uuid.UUID = Field(
        description="Идентификатор пользователя. Обязан совпадать с `sub` JWT."
    )
    sessionId: uuid.UUID | None = Field(
        default=None, description="Идентификатор сессии (опционально)."
    )
    # idempotency key (messageStepId for chat-debit)
    requestId: str = Field(
        min_length=1,
        description="Ключ идемпотентности списания (для chat-debit — `messageStepId`).",
    )
    amount: int = Field(gt=0, description="Сколько кредитов списать (> 0).")
    meta: dict[str, Any] = Field(
        default_factory=dict, description="Произвольные метаданные списания."
    )


class WalletConsumeResponse(StrictModel):
    newBalance: int = Field(description="Баланс кредитов после списания.")
    ledgerTxId: uuid.UUID = Field(description="Идентификатор созданной транзакции списания.")
