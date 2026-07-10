"""Wallet routes: GET /v1/wallet, POST /v1/wallet/consume (wallet-ledger/02)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.config import get_settings
from app.deps import CurrentUser, get_wallet_service, require_owner
from app.errors import RateLimitedError
from app.schemas.wallet import (
    LedgerTxView,
    WalletConsumeRequest,
    WalletConsumeResponse,
    WalletResponse,
)
from app.wallet.service import WalletService

router = APIRouter(prefix="/v1/wallet", tags=["Wallet"])

_CONSUME_REQUEST_EXAMPLES = {
    "debit_one": {
        "summary": "Списать 1 кредит",
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "sessionId": "3f1c2a7e-9b54-4d2e-8a11-6c0d5e7f1a23",
            "requestId": "msg-step-7c0d5e7f-1a23",
            "amount": 1,
            "meta": {"reason": "chat_message"},
        },
    },
}

_CONSUME_RESPONSE_EXAMPLES = {
    "ok": {
        "summary": "Успешное списание",
        "value": {
            "newBalance": 999,
            "ledgerTxId": "c1d2e3f4-5061-7283-94a5-b6c7d8e9f001",
        },
    },
}


@router.get(
    "",
    response_model=WalletResponse,
    summary="Получить баланс и историю",
    description=(
        "Возвращает текущий баланс кредитов и последние транзакции реестра (ledger). "
        "Количество транзакций ограничено конфигом сервера."
    ),
)
async def get_wallet(
    current: CurrentUser,
    wallet: Annotated[WalletService, Depends(get_wallet_service)],
) -> WalletResponse:
    last_n = get_settings().wallet_last_transactions
    balance, txs = await wallet.get_wallet_view(current.user_id, last_n)
    return WalletResponse(
        balance=balance,
        lastTransactions=[
            LedgerTxView(
                id=tx.id,
                type=tx.type,
                amount=tx.amount,
                createdAt=tx.created_at,
                meta=tx.meta,
            )
            for tx in txs
        ],
    )


@router.post(
    "/consume",
    response_model=WalletConsumeResponse,
    summary="Списать кредиты",
    description=(
        "Списывает кредиты с баланса пользователя. Идемпотентно по `requestId`: повторный "
        "вызов с тем же ключом и тем же payload не спишет дважды и вернёт тот же результат "
        "(конфликт ключа с другим payload — `409`)."
    ),
    responses={200: {"content": {"application/json": {"examples": _CONSUME_RESPONSE_EXAMPLES}}}},
)
async def consume(
    request: Request,
    current: CurrentUser,
    wallet: Annotated[WalletService, Depends(get_wallet_service)],
    body: Annotated[WalletConsumeRequest, Body(openapi_examples=_CONSUME_REQUEST_EXAMPLES)],
) -> WalletConsumeResponse:
    require_owner(body.userId, current)
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    result = await wallet.consume(
        user_id=current.user_id,
        amount=body.amount,
        idempotency_key=body.requestId,
        meta=body.meta,
        session_id=body.sessionId,
    )
    return WalletConsumeResponse(newBalance=result.new_balance, ledgerTxId=result.ledger_tx_id)
