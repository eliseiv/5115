"""Integration: ADR-053 (unchanged by ADR-054) — two-step userId resolution in the CP webhook.

The RU flow (broadapps) sends a **deviceId** as ``AccountId``/``Data.user_id``, NOT our internal
``userId``. ADR-053 resolves the callback identifier ``X`` in two steps: (a) ``X`` in ``users`` ->
it IS our userId (``resolvedVia="user_id"``); (b) else ``X`` in ``auth_devices.device_id`` -> take
the linked ``user_id`` (deviceId -> userId, ``resolvedVia="device_id"``); else -> user_not_found.
ADR-054 keeps resolution UNCHANGED but crediting now flows from broadapps VERIFICATION: all accrual
(subscription / wallet / ledger / dedup / audit) keys on the RESOLVED userId and on the broadapps
``payment_id`` — never on the deviceId, never on the callback ``TransactionId``.

Drives ``CloudPaymentsWebhookService.handle()`` DIRECTLY with a FAKE verify client (no network)
against the REAL testcontainers Postgres (seeded via ``seed_user`` + a direct ``auth_devices``
insert). In-process Alembic disables the service logger under ``caplog`` -> re-enabled below.
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
from app.observability.logging import JsonFormatter
from app.wallet.service import WalletService
from tests.conftest import seed_user

_MESSAGE = "cloudpayments_webhook_outcome"
_LOGGER = "app.billing_cloudpayments.service"
_SUB_CODE = "week_6.99_nottrial"
_SUB_TOKENS = 12000
_TOKEN_CODE = "100_tokens_9.99"
_TOKEN_CREDITS = 100

# U is our internal userId (in ``users``); D is a deviceId (in ``auth_devices.device_id`` -> U) that
# is NOT itself a ``users`` row.
_U = uuid.UUID("b0f407bd-4a19-449e-beab-84ce341d6915")
_D = uuid.UUID("55cbe083-fcbd-4460-af62-06f9a7bea97c")


class FakeVerifyClient:
    def __init__(self, *, payments: list[dict[str, Any]] | None = None) -> None:
        self._payments = payments if payments is not None else []
        self.calls: list[uuid.UUID] = []

    async def list_payments(self, *, device_id: uuid.UUID) -> list[dict[str, Any]]:
        self.calls.append(device_id)
        return [dict(p) for p in self._payments]


def _payment(payment_id: str, *, code: str, payment_type: str) -> dict[str, Any]:
    return {
        "payment_id": payment_id,
        "status": "succeeded",
        "paid_at": (
            datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(minutes=5)
        ).isoformat(),
        "product": {"code": code, "payment_type": payment_type},
    }


def _settings() -> Settings:
    return Settings(
        CLOUDPAYMENTS_API_TOKEN="verify-token-secret",  # noqa: S106 - test-only; config gate must pass
        CLOUDPAYMENTS_PRODUCT_TOKENS=json.dumps({_SUB_CODE: _SUB_TOKENS}),
        CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT=1000,
        TOKEN_PRODUCTS=json.dumps({_TOKEN_CODE: _TOKEN_CREDITS}),
    )


def _service(session: AsyncSession, verify: FakeVerifyClient) -> CloudPaymentsWebhookService:
    audit = AuditService(session)
    return CloudPaymentsWebhookService(
        session, WalletService(session, audit), audit, _settings(), verify
    )


def _body(*, account_id: str) -> bytes:
    body = {
        "Status": "Completed",
        "OperationType": "Payment",
        "Amount": 3990,
        "Currency": "RUB",
        "AccountId": account_id,
    }
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
) -> tuple[str, str | None] | None:
    async with maker() as s:
        row = (
            await s.execute(
                text("SELECT status, plan FROM subscriptions WHERE user_id=:u"), {"u": str(uid)}
            )
        ).first()
    return None if row is None else (row.status, row.plan)


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


async def _event_user_ids(maker: async_sessionmaker[AsyncSession], txn: str) -> list[str]:
    async with maker() as s:
        rows = (
            await s.execute(
                text("SELECT user_id FROM cloudpayments_webhook_events WHERE transaction_id=:t"),
                {"t": txn},
            )
        ).all()
    return [str(r.user_id) for r in rows]


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == _MESSAGE]


def _rendered(record: logging.LogRecord) -> dict[str, Any]:
    return json.loads(JsonFormatter().format(record))


@pytest.fixture(autouse=True)
def _enable_service_logger() -> None:
    logging.getLogger(_LOGGER).disabled = False


# ============================ §1 device-resolve (the core fix) ============================


@pytest.mark.asyncio
async def test_device_resolve_subscription_credits_the_linked_user(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # U is our user; auth_devices[D] -> U; the callback carries D (a deviceId, NOT in users). verify
    # is queried on D and returns a succeeded subscription; accrual lands on U (resolved), NOT D.
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)
    caplog.set_level(logging.DEBUG)

    verify = FakeVerifyClient(
        payments=[_payment("sub-1", code=_SUB_CODE, payment_type="subscription")]
    )
    outcome = await _service(db_session, verify).handle(_body(account_id=str(_D)))
    assert outcome.result == "applied"
    assert verify.calls == [_D]  # verified the deviceId, not the resolved userId

    assert await _subscription(db_sessionmaker, _U) == ("active", _SUB_CODE)
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    assert await _ledger_keys(db_sessionmaker, _U) == ["cp-txn:sub-1"]
    # D never appears as a user_id anywhere.
    assert await _subscription(db_sessionmaker, _D) is None
    assert await _balance(db_sessionmaker, _D) is None
    assert await _ledger_keys(db_sessionmaker, _D) == []
    assert await _event_user_ids(db_sessionmaker, "sub-1") == [str(_U)]

    fields = _rendered(_outcomes(caplog)[0])
    assert fields["resolvedVia"] == "device_id"
    assert fields["userId"] == str(_U)


@pytest.mark.asyncio
async def test_device_resolve_token_package_credits_the_linked_user(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)

    verify = FakeVerifyClient(
        payments=[_payment("tok-1", code=_TOKEN_CODE, payment_type="one_time")]
    )
    outcome = await _service(db_session, verify).handle(_body(account_id=str(_D)))
    assert outcome.result == "applied"
    assert await _balance(db_sessionmaker, _U) == _TOKEN_CREDITS
    assert await _ledger_keys(db_sessionmaker, _U) == ["cp-txn:tok-1"]
    assert await _subscription(db_sessionmaker, _U) is None
    assert await _balance(db_sessionmaker, _D) is None


@pytest.mark.asyncio
async def test_device_resolve_uppercase_account_id_normalised_and_matched(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # auth_devices stores the lower deviceId; the callback sends it UPPER -> normalised -> matched.
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)
    caplog.set_level(logging.DEBUG)

    verify = FakeVerifyClient(
        payments=[_payment("sub-9", code=_SUB_CODE, payment_type="subscription")]
    )
    outcome = await _service(db_session, verify).handle(_body(account_id=str(_D).upper()))
    assert outcome.result == "applied"
    # verify is queried on the NORMALISED (lower) canonical deviceId.
    assert verify.calls == [_D]
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    assert _rendered(_outcomes(caplog)[0])["resolvedVia"] == "device_id"


@pytest.mark.asyncio
async def test_device_resolve_idempotent_replay(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    await _seed_device(db_sessionmaker, device_id=str(_D), user_id=_U)
    verify = FakeVerifyClient(
        payments=[_payment("sub-1", code=_SUB_CODE, payment_type="subscription")]
    )

    first = await _service(db_session, verify).handle(_body(account_id=str(_D)))
    assert first.result == "applied"
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS

    caplog.set_level(logging.DEBUG)
    second = await _service(db_session, verify).handle(_body(account_id=str(_D)))
    assert second.result == "duplicate"
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS  # unchanged on replay
    assert await _ledger_keys(db_sessionmaker, _U) == ["cp-txn:sub-1"]
    assert _rendered(_outcomes(caplog)[0])["resolvedVia"] == "device_id"


# ============================ §1a user-resolve (backward compat) ============================


@pytest.mark.asyncio
async def test_user_resolve_direct_userid_still_works(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # AccountId IS our userId (present in users) -> path (a), resolvedVia="user_id", accrual on U.
    await seed_user(db_session, user_id=_U)
    await db_session.commit()
    caplog.set_level(logging.DEBUG)

    verify = FakeVerifyClient(
        payments=[_payment("sub-1", code=_SUB_CODE, payment_type="subscription")]
    )
    outcome = await _service(db_session, verify).handle(_body(account_id=str(_U)))
    assert outcome.result == "applied"
    assert verify.calls == [_U]
    assert await _subscription(db_sessionmaker, _U) == ("active", _SUB_CODE)
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    fields = _rendered(_outcomes(caplog)[0])
    assert fields["resolvedVia"] == "user_id"
    assert fields["userId"] == str(_U)


@pytest.mark.asyncio
async def test_user_resolve_takes_priority_over_device(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # ADR-053 §4: if X is BOTH a users row AND an auth_devices.device_id, users wins (a before b).
    other = uuid.uuid4()
    await seed_user(db_session, user_id=_U)
    await seed_user(db_session, user_id=other)
    await db_session.commit()
    await _seed_device(db_sessionmaker, device_id=str(_U), user_id=other)
    caplog.set_level(logging.DEBUG)

    verify = FakeVerifyClient(
        payments=[_payment("sub-1", code=_SUB_CODE, payment_type="subscription")]
    )
    outcome = await _service(db_session, verify).handle(_body(account_id=str(_U)))
    assert outcome.result == "applied"
    assert _rendered(_outcomes(caplog)[0])["resolvedVia"] == "user_id"
    assert await _balance(db_sessionmaker, _U) == _SUB_TOKENS
    assert await _balance(db_sessionmaker, other) is None  # the device-linked user is NOT credited


# ==================== §1c not found (no provisioning, no verify GET) ====================


@pytest.mark.asyncio
async def test_unresolvable_identifier_is_user_not_found_no_verify(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A UUID in neither users nor auth_devices -> user_not_found (WARNING); verify NOT called.
    stranger = uuid.uuid4()
    await seed_user(db_session, user_id=_U)  # unrelated user
    await db_session.commit()
    caplog.set_level(logging.DEBUG)

    verify = FakeVerifyClient(
        payments=[_payment("sub-1", code=_SUB_CODE, payment_type="subscription")]
    )
    outcome = await _service(db_session, verify).handle(_body(account_id=str(stranger).upper()))
    assert (outcome.result, outcome.reason) == ("ignored", "user_not_found")
    assert verify.calls == []  # no outgoing GET for an unresolvable deviceId
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    fields = _rendered(recs[0])
    assert "resolvedVia" not in fields
    assert "userId" not in fields
    assert await _balance(db_sessionmaker, stranger) is None
    assert await _balance(db_sessionmaker, _U) is None
