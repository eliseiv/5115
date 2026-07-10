"""Admin schemas for /v1/admin/* (admin/02-api-contracts.md, ADR-009).

Strict Pydantic v2 (extra='forbid'): amount > 0, non-empty reason, bounded idempotencyKey.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Literal

from pydantic import Field, model_validator

from app.schemas.common import StrictModel


class AdminGrantRequest(StrictModel):
    userId: uuid.UUID = Field(description="Идентификатор существующего пользователя.")
    amount: int = Field(gt=0, description="Сколько кредитов начислить (целое > 0).")
    idempotencyKey: str = Field(
        min_length=1,
        max_length=128,
        description="Ключ идемпотентности начисления.",
    )
    reason: str = Field(
        min_length=1,
        max_length=512,
        description="Причина начисления (обязательна).",
    )


class AdminGrantResponse(StrictModel):
    newBalance: int = Field(description="Баланс кредитов после начисления.")
    ledgerTxId: uuid.UUID = Field(description="Идентификатор транзакции реестра.")
    idempotentReplay: bool = Field(
        description="true, если ключ уже использовался с тем же payload (повтор без начисления)."
    )


class AdminSubscriptionGrantRequest(StrictModel):
    userId: uuid.UUID = Field(description="Идентификатор существующего пользователя.")
    expiresAt: datetime.datetime | None = Field(
        default=None,
        description=(
            "Точный момент истечения подписки (с указанием часового пояса, строго в будущем). "
            "Задаётся вместо `days`."
        ),
    )
    days: int | None = Field(
        default=None,
        gt=0,
        description="Срок действия в днях от текущего момента. Задаётся вместо `expiresAt`.",
    )
    plan: str | None = Field(
        default="manual_grant",
        max_length=128,
        description="Метка тарифного плана.",
    )
    idempotencyKey: str = Field(
        min_length=1,
        max_length=128,
        description="Ключ идемпотентности начисления кредитов.",
    )
    credits: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Сколько кредитов начислить вместе с активацией. Если не указано — "
            "начисляется стандартный пакет периода; `0` — активировать без начисления."
        ),
    )

    @model_validator(mode="after")
    def _check_term(self) -> AdminSubscriptionGrantRequest:
        """Exactly one of expiresAt/days; expiresAt must be tz-aware and strictly future.

        ADR-048 §1: lazy expiry (policy/loader) treats a past expires_at as expired, so a
        grant into the past would silently fail to unblock — hence the strict-future rule.
        """
        has_expires = self.expiresAt is not None
        has_days = self.days is not None
        if has_expires == has_days:
            raise ValueError("provide exactly one of 'expiresAt' or 'days'")
        if self.expiresAt is not None:
            if self.expiresAt.utcoffset() is None:
                raise ValueError("'expiresAt' must be timezone-aware")
            if self.expiresAt <= datetime.datetime.now(tz=datetime.UTC):
                raise ValueError("'expiresAt' must be in the future")
        return self


class AdminSubscriptionGrantResponse(StrictModel):
    status: str = Field(description="Текущий статус подписки.")
    expiresAt: datetime.datetime | None = Field(description="Момент истечения подписки.")
    plan: str | None = Field(description="Записанный тарифный план.")
    creditsGranted: int = Field(
        description="Сколько кредитов начислено (0, если начисление не выполнялось)."
    )
    newBalance: int | None = Field(
        default=None,
        description="Баланс кредитов после начисления; отсутствует, если кредиты не начислялись.",
    )
    ledgerTxId: uuid.UUID | None = Field(
        default=None,
        description="Идентификатор кредитной транзакции; отсутствует, если начисления не было.",
    )
    idempotentReplay: bool | None = Field(
        default=None,
        description=(
            "Признак повторного начисления по тому же ключу; отсутствует, если начисления не было."
        ),
    )


class AdminLedgerTxView(StrictModel):
    id: uuid.UUID = Field(description="Идентификатор транзакции реестра.")
    type: Literal["credit", "debit"] = Field(description="Тип транзакции.")
    amount: int = Field(description="Сумма транзакции в кредитах.")
    createdAt: datetime.datetime = Field(description="Время создания (UTC).")
    meta: dict[str, Any] = Field(description="Метаданные (без секретов).")


class AdminWalletResponse(StrictModel):
    userId: uuid.UUID = Field(description="Идентификатор пользователя.")
    balance: int = Field(description="Текущий баланс кредитов.")
    lastTransactions: list[AdminLedgerTxView] = Field(
        description="Последние транзакции реестра (новые первыми)."
    )
