"""Integration: ADR-054 — callback = TRIGGER -> broadapps verify -> reconcile -> credit.

Drives ``CloudPaymentsWebhookService.handle()`` DIRECTLY against the REAL testcontainers Postgres
with a FAKE verify client (``list_payments`` returns scripted broadapps ``data[]`` or raises a
scripted error) — so NO network to broadapps and the outgoing GET is fully controlled. Covers the
ADR-054 verification model end-to-end at the service seam:

- config gate (missing ``CLOUDPAYMENTS_API_TOKEN`` -> 500 misconfigured, before any parse/GET);
- the main verify path (fresh ``succeeded`` -> ``applied``, ``creditedCount``, credit on the
  RESOLVED user, idempotency by ``cp-txn:{payment_id}``, replay -> ``duplicate``);
- class by authoritative ``product.payment_type`` (one_time -> tokens, subscription -> sub+credits,
  unknown -> skipped WARNING, not credited);
- freshness window (stale ``succeeded`` -> ``no_creditable_payment``);
- verify transient error -> 500 retriable (no credit); broadapps ``404`` (fake ``[]``) ->
  ``no_creditable_payment`` (NOT 500);
- reconciliation of many payments (creditedCount == new ones; mixed new+duplicate -> applied);
- anti-tamper (broadapps ``amount`` never sizes the grant — server card by ``product.code``);
- ``user_not_found`` -> verify is NEVER called, no credit;
- PII: the outcome log carries only the allowlist (no amount/currency/email/card/token).

Hermetic: DB is the shared container (seeded via ``seed_user`` + direct ``auth_devices`` insert);
``Settings`` inline; no network; no LLM. In-process Alembic disables the service logger under
``caplog`` -> re-enabled in the fixture.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.billing_cloudpayments.service import CloudPaymentsWebhookService
from app.config import Settings
from app.errors import (
    CloudPaymentsVerificationUnavailableError,
    CloudPaymentsWebhookMisconfiguredError,
)
from app.observability.logging import JsonFormatter
from app.wallet.service import WalletService
from tests.conftest import seed_user

_MESSAGE = "cloudpayments_webhook_outcome"
_SKIPPED = "cloudpayments_payment_skipped"
_LOGGER = "app.billing_cloudpayments.service"

_SUB_CODE = "week_6.99_nottrial"  # infer_interval_unit_from_code -> "week" (7d expiry)
_SUB_TOKENS = 12000
_FALLBACK = 1000
_TOKEN_CODE = "100_tokens_9.99"
_TOKEN_CREDITS = 100

# U is our internal userId (in ``users``); D is a deviceId (in ``auth_devices.device_id`` -> U).
_U = uuid.UUID("b0f407bd-4a19-449e-beab-84ce341d6915")
_D = uuid.UUID("55cbe083-fcbd-4460-af62-06f9a7bea97c")


# --------------------------- fake verify client ---------------------------


class FakeVerifyClient:
    """Stand-in for ``CloudPaymentsVerifyClient`` — scripted ``data[]`` or a scripted raise.

    Records each ``device_id`` it was queried with so tests can assert the outgoing GET was (or was
    NOT, e.g. on ``user_not_found``) attempted, and on WHICH device id (SSRF-safe canonical UUID).
    """

    def __init__(
        self, *, payments: list[dict[str, Any]] | None = None, error: Exception | None = None
    ) -> None:
        self._payments = payments if payments is not None else []
        self._error = error
        self.calls: list[uuid.UUID] = []

    def set_payments(self, payments: list[dict[str, Any]]) -> None:
        self._payments = payments
        self._error = None

    async def list_payments(self, *, device_id: uuid.UUID) -> list[dict[str, Any]]:
        self.calls.append(device_id)
        if self._error is not None:
            raise self._error
        return [dict(p) for p in self._payments]


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def _fresh_iso(minutes: int = 5) -> str:
    return (_now() - datetime.timedelta(minutes=minutes)).isoformat()


def _payment(
    payment_id: str,
    *,
    code: str = _TOKEN_CODE,
    payment_type: str = "one_time",
    status: str = "succeeded",
    paid_at: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "payment_id": payment_id,
        "status": status,
        "paid_at": paid_at if paid_at is not None else _fresh_iso(),
        "product": {"code": code, "payment_type": payment_type},
    }
    p.update(extra)
    return p


def _settings(**over: Any) -> Settings:
    kwargs: dict[str, Any] = {
        "CLOUDPAYMENTS_API_TOKEN": "verify-token-secret",  # config gate: set (avelyra)
        "CLOUDPAYMENTS_PRODUCT_TOKENS": json.dumps({_SUB_CODE: _SUB_TOKENS}),
        "CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT": _FALLBACK,
        "TOKEN_PRODUCTS": json.dumps({_TOKEN_CODE: _TOKEN_CREDITS}),
    }
    kwargs.update(over)
    return Settings(**kwargs)


def _service(
    session: AsyncSession, verify: FakeVerifyClient, settings: Settings | None = None
) -> CloudPaymentsWebhookService:
    audit = AuditService(session)
    return CloudPaymentsWebhookService(
        session, WalletService(session, audit), audit, settings or _settings(), verify
    )


def _body(
    *,
    account_id: str | None,
    status: str = "Completed",
    operation_type: str = "Payment",
    transaction_id: str | None = None,
    amount: int = 3990,
    data: dict[str, Any] | str | None = None,
) -> bytes:
    body: dict[str, Any] = {
        "Status": status,
        "OperationType": operation_type,
        "Amount": amount,
        "Currency": "RUB",
    }
    if account_id is not None:
        body["AccountId"] = account_id
    if transaction_id is not None:
        body["TransactionId"] = transaction_id
    if data is not None:
        body["Data"] = json.dumps(data) if isinstance(data, dict) else data
    return json.dumps(body).encode()


# --------------------------- DB helpers ---------------------------


async def _seed_device(
    maker: async_sessionmaker[AsyncSession], *, device_id: str, user_id: uuid.UUID
) -> None:
    async with maker() as s:
        await s.execute(
            text("INSERT INTO auth_devices (device_id, user_id) VALUES (:d, :u)"),
            {"d": device_id, "u": str(user_id)},
        )
        await s.commit()


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int | None:
    async with maker() as s:
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
        return None if bal is None else int(bal)


async def _subscription(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID
) -> tuple[str, str | None, Any] | None:
    async with maker() as s:
        row = (
            await s.execute(
                text("SELECT status, plan, expires_at FROM subscriptions WHERE user_id=:u"),
                {"u": str(uid)},
            )
        ).first()
    return None if row is None else (row.status, row.plan, row.expires_at)


async def _ledger_keys(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> list[str]:
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT idempotency_key FROM ledger_transactions WHERE user_id=:u "
                    "ORDER BY created_at"
                ),
                {"u": str(uid)},
            )
        ).all()
    return [r.idempotency_key for r in rows]


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == _MESSAGE]


def _skips(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == _SKIPPED]


def _rendered(record: logging.LogRecord) -> dict[str, Any]:
    return json.loads(JsonFormatter().format(record))


@pytest.fixture(autouse=True)
def _enable_service_logger() -> None:
    logging.getLogger(_LOGGER).disabled = False


# ============================ §2 config gate (before parse/GET) ============================


@pytest.mark.asyncio
async def test_missing_api_token_is_500_misconfigured_before_verify(
    db_session: AsyncSession,
) -> None:
    verify = FakeVerifyClient(payments=[_payment("p1")])
    svc = _service(db_session, verify, _settings(CLOUDPAYMENTS_API_TOKEN=""))
    with pytest.raises(CloudPaymentsWebhookMisconfiguredError) as exc:
        await svc.handle(_body(account_id=str(_U)))
    assert exc.value.status_code == 500
    # The gate is BEFORE any outgoing GET (a forged callback cannot use broadapps as an oracle).
    assert verify.calls == []


# ============================ §Verify main path ============================


@pytest.mark.asyncio
async def test_fresh_succeeded_one_time_payment_is_applied_and_credits_resolved_user(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)
    caplog.set_level(logging.DEBUG)

    verify = FakeVerifyClient(
        payments=[_payment("pay-1", code=_TOKEN_CODE, payment_type="one_time")]
    )
    svc = _service(db_session, verify)
    outcome = await svc.handle(_body(account_id=str(_D)))
    assert outcome.result == "applied"

    # verify queried the deviceId D (canonical UUID); credit is on U (resolved), NOT D.
    assert verify.calls == [_D]
    assert await _balance(db_sessionmaker, _U) == _TOKEN_CREDITS
    assert await _ledger_keys(db_sessionmaker, _U) == ["cp-txn:pay-1"]
    assert await _balance(db_sessionmaker, _D) is None

    fields = _rendered(_outcomes(caplog)[0])
    assert fields["result"] == "applied"
    assert fields["verify"] == "ok"
    assert fields["creditedCount"] == 1
    assert fields["resolvedVia"] == "device_id"
    assert fields["userId"] == str(_U)
    assert fields["paymentStatuses"] == ["succeeded"]


@pytest.mark.asyncio
async def test_subscription_payment_type_activates_sub_and_credits(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()

    verify = FakeVerifyClient(
        payments=[_payment("sub-1", code=_SUB_CODE, payment_type="subscription")]
    )
    svc = _service(db_session, verify)
    outcome = await svc.handle(_body(account_id=str(_U)))  # X in users -> resolvedVia=user_id
    assert outcome.result == "applied"

    sub = await _subscription(db_sessionmaker, _U)
    assert sub is not None
    status, plan, expires_at = sub
    assert status == "active"
    assert plan == _SUB_CODE
    delta_days = (expires_at - _now()).days
    assert 6 <= delta_days <= 8, delta_days  # week interval inferred from the code
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    assert await _ledger_keys(db_sessionmaker, _U) == ["cp-txn:sub-1"]


@pytest.mark.asyncio
async def test_subscription_unmapped_code_uses_fallback_grant(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    # A subscription code NOT in CLOUDPAYMENTS_PRODUCT_TOKENS -> fallback grant.
    verify = FakeVerifyClient(
        payments=[_payment("sub-2", code="month_1.99_nottrial", payment_type="subscription")]
    )
    outcome = await _service(db_session, verify).handle(_body(account_id=str(_U)))
    assert outcome.result == "applied"
    assert await _balance(db_sessionmaker, _U) == _FALLBACK


@pytest.mark.asyncio
async def test_replay_same_payment_id_is_duplicate_no_double_credit(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    verify = FakeVerifyClient(payments=[_payment("pay-1")])

    first = await _service(db_session, verify).handle(_body(account_id=str(_U)))
    assert first.result == "applied"
    assert await _balance(db_sessionmaker, _U) == _TOKEN_CREDITS

    caplog.set_level(logging.DEBUG)
    second = await _service(db_session, verify).handle(_body(account_id=str(_U)))
    assert second.result == "duplicate"
    # Idempotent: balance/ledger unchanged on replay (double boundary: event dedup + ledger key).
    assert await _balance(db_sessionmaker, _U) == _TOKEN_CREDITS
    assert await _ledger_keys(db_sessionmaker, _U) == ["cp-txn:pay-1"]
    fields = _rendered(_outcomes(caplog)[0])
    assert fields["result"] == "duplicate"
    assert fields["creditedCount"] == 0


# ============================ §Class by payment_type ============================


@pytest.mark.asyncio
async def test_unknown_payment_type_is_skipped_not_credited(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    verify = FakeVerifyClient(
        payments=[_payment("pay-x", code=_TOKEN_CODE, payment_type="donation")]
    )
    outcome = await _service(db_session, verify).handle(_body(account_id=str(_U)))
    # No credit; a per-payment WARNING skip is emitted (unknown_payment_type).
    assert await _balance(db_sessionmaker, _U) is None
    assert await _ledger_keys(db_sessionmaker, _U) == []
    assert outcome.result == "duplicate" and outcome.reason is None
    skips = _skips(caplog)
    assert len(skips) == 1
    assert skips[0].levelno == logging.WARNING
    assert _rendered(skips[0])["reason"] == "unknown_payment_type"


@pytest.mark.asyncio
async def test_unknown_product_code_one_time_is_skipped_not_credited(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    # one_time but product.code absent from TOKEN_PRODUCTS -> anti-tamper skip (never guess N).
    verify = FakeVerifyClient(
        payments=[_payment("pay-y", code="999_tokens_pack", payment_type="one_time")]
    )
    await _service(db_session, verify).handle(_body(account_id=str(_U)))
    assert await _balance(db_sessionmaker, _U) is None
    skips = _skips(caplog)
    assert len(skips) == 1 and _rendered(skips[0])["reason"] == "unknown_product"


# ============================ §Freshness window ============================


@pytest.mark.asyncio
async def test_stale_succeeded_payment_is_no_creditable_payment(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    stale = _payment("old-1", paid_at=(_now() - datetime.timedelta(hours=100)).isoformat())
    verify = FakeVerifyClient(payments=[stale])
    outcome = await _service(db_session, verify).handle(_body(account_id=str(_U)))
    assert (outcome.result, outcome.reason) == ("ignored", "no_creditable_payment")
    assert await _balance(db_sessionmaker, _U) is None
    rec = _outcomes(caplog)[0]
    assert rec.levelno == logging.WARNING
    fields = _rendered(rec)
    assert fields["verify"] == "ok"
    assert fields["creditedCount"] == 0
    # Raw statuses are still logged (Q-054-1 calibration) even when nothing was creditable.
    assert fields["paymentStatuses"] == ["succeeded"]


# ============================ §Verify errors ============================


@pytest.mark.asyncio
async def test_verify_transient_error_raises_500_no_credit(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    verify = FakeVerifyClient(error=CloudPaymentsVerificationUnavailableError("unavailable"))
    with pytest.raises(CloudPaymentsVerificationUnavailableError):
        await _service(db_session, verify).handle(_body(account_id=str(_U)))
    assert await _balance(db_sessionmaker, _U) is None


@pytest.mark.asyncio
async def test_broadapps_404_empty_is_no_creditable_payment_not_500(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    # broadapps 404 -> the client returns [] (permanent "no payments"), NOT a transient error.
    verify = FakeVerifyClient(payments=[])
    outcome = await _service(db_session, verify).handle(_body(account_id=str(_U)))
    assert (outcome.result, outcome.reason) == ("ignored", "no_creditable_payment")
    assert verify.calls == [_U]
    assert await _balance(db_sessionmaker, _U) is None


# ============================ §Reconciliation of many payments ============================


@pytest.mark.asyncio
async def test_multiple_fresh_succeeded_credit_each_new_payment(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    verify = FakeVerifyClient(
        payments=[
            _payment("m1", code=_TOKEN_CODE, payment_type="one_time"),
            _payment("m2", code=_TOKEN_CODE, payment_type="one_time"),
            _payment("m3", code=_SUB_CODE, payment_type="subscription"),
        ]
    )
    outcome = await _service(db_session, verify).handle(_body(account_id=str(_U)))
    assert outcome.result == "applied"
    assert set(await _ledger_keys(db_sessionmaker, _U)) == {"cp-txn:m1", "cp-txn:m2", "cp-txn:m3"}
    assert await _balance(db_sessionmaker, _U) == 2 * _TOKEN_CREDITS + _SUB_TOKENS
    assert _rendered(_outcomes(caplog)[0])["creditedCount"] == 3


@pytest.mark.asyncio
async def test_mixed_new_and_duplicate_is_applied_with_only_new_counted(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    verify = FakeVerifyClient(payments=[_payment("a1")])
    assert (
        await _service(db_session, verify).handle(_body(account_id=str(_U)))
    ).result == "applied"

    caplog.set_level(logging.DEBUG)
    # Second callback: A1 already credited (duplicate) + a new B1 -> applied, creditedCount=1.
    verify.set_payments([_payment("a1"), _payment("b1")])
    outcome = await _service(db_session, verify).handle(_body(account_id=str(_U)))
    assert outcome.result == "applied"
    assert set(await _ledger_keys(db_sessionmaker, _U)) == {"cp-txn:a1", "cp-txn:b1"}
    assert await _balance(db_sessionmaker, _U) == 2 * _TOKEN_CREDITS
    assert _rendered(_outcomes(caplog)[0])["creditedCount"] == 1


# ============================ §Anti-tamper ============================


@pytest.mark.asyncio
async def test_broadapps_amount_never_sizes_the_grant(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    # A huge broadapps amount + huge callback Amount: the grant must equal the server card (100).
    verify = FakeVerifyClient(
        payments=[_payment("pay-1", code=_TOKEN_CODE, payment_type="one_time", amount="9999999.00")]
    )
    outcome = await _service(db_session, verify).handle(
        _body(account_id=str(_U), amount=999_999_999)
    )
    assert outcome.result == "applied"
    assert await _balance(db_sessionmaker, _U) == _TOKEN_CREDITS


# ============================ §user_not_found (no GET) ============================


@pytest.mark.asyncio
async def test_user_not_found_skips_verify_and_credits_nothing(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A valid UUID present neither in users nor auth_devices -> user_not_found, verify NEVER called.
    await seed_user(db_session, user_id=_U)  # unrelated user exists
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    stranger = uuid.uuid4()
    verify = FakeVerifyClient(payments=[_payment("pay-1")])
    outcome = await _service(db_session, verify).handle(_body(account_id=str(stranger).upper()))
    assert (outcome.result, outcome.reason) == ("ignored", "user_not_found")
    assert verify.calls == []  # no outgoing GET for an unresolvable deviceId
    assert await _balance(db_sessionmaker, stranger) is None
    rec = _outcomes(caplog)[0]
    assert rec.levelno == logging.WARNING
    fields = _rendered(rec)
    # Pre-verify outcome: verify/creditedCount/paymentStatuses and resolvedVia/userId are omitted.
    assert "verify" not in fields
    assert "creditedCount" not in fields
    assert "resolvedVia" not in fields
    assert "userId" not in fields


# ============================ §10 PII: outcome log allowlist ============================


@pytest.mark.asyncio
async def test_outcome_log_carries_only_allowlist_no_pii(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)
    # broadapps payment carries amount/currency/email + card-ish fields; NONE may reach the log.
    payment = _payment(
        "pay-1",
        code=_TOKEN_CODE,
        payment_type="one_time",
        amount="799.00",
        currency="RUB",
        customer_email="buyer@example.com",
        card_first_six="220024",
    )
    verify = FakeVerifyClient(payments=[payment])
    await _service(db_session, verify).handle(
        _body(account_id=str(_U), data={"authorization": "Bearer super-secret-DO-NOT-LOG"})
    )

    recs = _outcomes(caplog)
    assert len(recs) == 1  # exactly one aggregate outcome per callback
    fields = _rendered(recs[0])
    blob = json.dumps(fields)
    for forbidden in ("799.00", "RUB-", "buyer@example.com", "220024", "super-secret-DO-NOT-LOG"):
        assert forbidden not in blob
    assert "bearer" not in blob.lower()
    assert set(fields) <= {
        "level",
        "logger",
        "message",
        "result",
        "reason",
        "transactionId",
        "userId",
        "resolvedVia",
        "verify",
        "creditedCount",
        "paymentStatuses",
        "requestId",
        "sessionId",
    }
