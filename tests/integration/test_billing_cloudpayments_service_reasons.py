"""Integration: ADR-054 — CloudPayments webhook outcome reasons, PII-safe logging, migration 0014.

Revised from ADR-050: the callback is a TRIGGER and crediting happens only after broadapps
verification, so the outcome reasons changed. The HTTP route collapses every processed outcome to
``{"code": 0}``, so the precise ``ignored/<reason>`` classification is asserted by driving
``CloudPaymentsWebhookService.handle()`` DIRECTLY against the REAL PostgreSQL container with a FAKE
verify client (no network). Hermetic: DB is the shared testcontainers Postgres (seeded via
``seed_user``); ``Settings`` inline; no LLM.

Covers the ADR-054 shape/gate/resolve reasons, ``no_creditable_payment`` (WARNING, post-verify),
``applied``/``duplicate``, the PII-safe single outcome log, the ``_level_for`` table, and migration
0014 (single head + ``transaction_id`` PRIMARY KEY, unchanged by ADR-054 — the column is repurposed
to hold the broadapps ``payment_id`` without DDL).
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.billing_cloudpayments.service import CloudPaymentsWebhookService, _level_for
from app.config import Settings
from app.observability.logging import JsonFormatter
from app.wallet.service import WalletService
from tests.conftest import seed_user

_MESSAGE = "cloudpayments_webhook_outcome"
_LOGGER = "app.billing_cloudpayments.service"
_TOKEN_CODE = "100_tokens_9.99"
_TOKEN_CREDITS = 100
_UID_UPPER = "B284721F-C3E0-4446-B00F-3C6A21F32535"
_SENTINEL = "Bearer super-secret-cloudpayments-token-DO-NOT-LOG"  # noqa: S105 - test sentinel


class FakeVerifyClient:
    """Scripted broadapps verify double: returns ``data[]``; records queried device ids."""

    def __init__(self, *, payments: list[dict[str, Any]] | None = None) -> None:
        self._payments = payments if payments is not None else []
        self.calls: list[uuid.UUID] = []

    async def list_payments(self, *, device_id: uuid.UUID) -> list[dict[str, Any]]:
        self.calls.append(device_id)
        return [dict(p) for p in self._payments]


def _fresh_payment(payment_id: str = "pay-1") -> dict[str, Any]:
    return {
        "payment_id": payment_id,
        "status": "succeeded",
        "paid_at": (
            datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(minutes=5)
        ).isoformat(),
        "product": {"code": _TOKEN_CODE, "payment_type": "one_time"},
    }


def _settings() -> Settings:
    return Settings(
        CLOUDPAYMENTS_API_TOKEN="verify-token-secret",  # noqa: S106 - test-only; config gate must pass
        TOKEN_PRODUCTS=json.dumps({_TOKEN_CODE: _TOKEN_CREDITS}),
    )


def _service(session: AsyncSession, verify: FakeVerifyClient) -> CloudPaymentsWebhookService:
    audit = AuditService(session)
    return CloudPaymentsWebhookService(
        session, WalletService(session, audit), audit, _settings(), verify
    )


@pytest.fixture
async def cp_service(db_session: AsyncSession) -> AsyncIterator[CloudPaymentsWebhookService]:
    """Service bound to the container DB (real Wallet/Audit, inline Settings, empty verify).

    Re-enable the service logger: the in-process Alembic migration (fileConfig default
    ``disable_existing_loggers=True``) disables ``app.billing_cloudpayments.service`` -> otherwise
    swallowed under ``caplog`` (test-harness artifact only).
    """
    logging.getLogger(_LOGGER).disabled = False
    yield _service(db_session, FakeVerifyClient(payments=[]))


def _body(
    *,
    data: dict[str, Any] | str | None = None,
    account_id: str | None = _UID_UPPER,
    status: str = "Completed",
    operation_type: str = "Payment",
    with_card: bool = False,
) -> bytes:
    body: dict[str, Any] = {"Status": status, "OperationType": operation_type, "Currency": "RUB"}
    if data is not None:
        body["Data"] = json.dumps(data) if isinstance(data, dict) else data
    if account_id is not None:
        body["AccountId"] = account_id
    if with_card:
        body.update(
            {"CardFirstSix": "220024", "CardLastFour": "8808", "Issuer": "VTB", "CardType": "Mir"}
        )
    return json.dumps(body).encode()


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == _MESSAGE]


def _rendered(record: logging.LogRecord) -> dict[str, Any]:
    return json.loads(JsonFormatter().format(record))


# ==================== Pre-verify shape / gate / resolve reasons ====================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw, reason, level",
    [
        (b"", "empty_body", logging.DEBUG),
        (b"not json <<<", "invalid_json", logging.INFO),
        (b"[1,2,3]", "not_an_object", logging.INFO),
    ],
)
async def test_body_shape_reasons(
    cp_service: CloudPaymentsWebhookService,
    caplog: pytest.LogCaptureFixture,
    raw: bytes,
    reason: str,
    level: int,
) -> None:
    caplog.set_level(logging.DEBUG)
    outcome = await cp_service.handle(raw)
    assert (outcome.result, outcome.reason) == ("ignored", reason)
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == level


@pytest.mark.asyncio
async def test_gate_not_a_completed_payment_reason(cp_service: CloudPaymentsWebhookService) -> None:
    outcome = await cp_service.handle(_body(status="Pending"))
    assert (outcome.result, outcome.reason) == ("ignored", "not_a_completed_payment")


@pytest.mark.asyncio
async def test_invalid_account_id_reason(cp_service: CloudPaymentsWebhookService) -> None:
    # Neither a valid AccountId nor Data.user_id -> invalid_account_id (no verify).
    outcome = await cp_service.handle(_body(account_id="not-a-uuid"))
    assert (outcome.result, outcome.reason) == ("ignored", "invalid_account_id")


@pytest.mark.asyncio
async def test_missing_account_id_and_user_id_reason(
    cp_service: CloudPaymentsWebhookService,
) -> None:
    outcome = await cp_service.handle(_body(account_id=None, data={"product_id": "x"}))
    assert (outcome.result, outcome.reason) == ("ignored", "invalid_account_id")


@pytest.mark.asyncio
async def test_transaction_id_and_product_id_no_longer_gate(
    cp_service: CloudPaymentsWebhookService, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # ADR-054: a callback WITHOUT TransactionId / product_id is still processed (they are now only
    # optional log context). With a resolvable user + empty verify -> no_creditable_payment (NOT an
    # early ignore on missing_transaction_id/missing_product_id, which ADR-054 removed).
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uuid.UUID(_UID_UPPER.lower()))
    outcome = await cp_service.handle(_body(data=None))  # no TransactionId, no Data.product_id
    assert (outcome.result, outcome.reason) == ("ignored", "no_creditable_payment")


@pytest.mark.asyncio
async def test_user_not_found_reason_and_warning(
    cp_service: CloudPaymentsWebhookService, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    uid = uuid.uuid4()  # valid UUID, no users / auth_devices row
    outcome = await cp_service.handle(_body(account_id=str(uid).upper()))
    assert (outcome.result, outcome.reason) == ("ignored", "user_not_found")
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    fields = _rendered(recs[0])
    # Pre-verify: X is only a candidate deviceId -> userId/resolvedVia/verify are omitted.
    assert "userId" not in fields
    assert "resolvedVia" not in fields
    assert "verify" not in fields


# ==================== Post-verify: no_creditable_payment (WARNING) ====================


@pytest.mark.asyncio
async def test_no_creditable_payment_reason_and_warning(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await seed_user(db_session, user_id=uuid.UUID(_UID_UPPER.lower()))
    await db_session.commit()
    logging.getLogger(_LOGGER).disabled = False
    caplog.set_level(logging.DEBUG)
    # Resolvable user, but broadapps returns no fresh succeeded payment (empty data).
    outcome = await _service(db_session, FakeVerifyClient(payments=[])).handle(_body(data=None))
    assert (outcome.result, outcome.reason) == ("ignored", "no_creditable_payment")
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    fields = _rendered(recs[0])
    assert fields["verify"] == "ok"
    assert fields["creditedCount"] == 0
    assert fields["resolvedVia"] == "user_id"


# ============================ Applied / duplicate ============================


@pytest.mark.asyncio
async def test_applied_then_duplicate(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await seed_user(db_session, user_id=uuid.UUID(_UID_UPPER.lower()))
    await db_session.commit()
    logging.getLogger(_LOGGER).disabled = False
    verify = FakeVerifyClient(payments=[_fresh_payment("pay-1")])
    caplog.set_level(logging.DEBUG)

    first = await _service(db_session, verify).handle(_body(data=None))
    assert first.result == "applied"
    assert len(_outcomes(caplog)) == 1
    assert _outcomes(caplog)[0].levelno == logging.INFO

    caplog.clear()
    second = await _service(db_session, verify).handle(_body(data=None))
    assert second.result == "duplicate"
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.INFO


# ============================ PII-safe log (no card data / bearer) ============================


@pytest.mark.asyncio
async def test_outcome_log_carries_no_card_data_or_secret(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    await seed_user(db_session, user_id=uuid.UUID(_UID_UPPER.lower()))
    await db_session.commit()
    logging.getLogger(_LOGGER).disabled = False
    caplog.set_level(logging.DEBUG)
    verify = FakeVerifyClient(payments=[_fresh_payment("pay-1")])
    # Plant card data + a bearer-like sentinel in the callback body; the allowlist must drop them.
    await _service(db_session, verify).handle(
        _body(data={"authorization": _SENTINEL}, with_card=True)
    )

    recs = _outcomes(caplog)
    assert len(recs) == 1
    rendered = json.dumps(_rendered(recs[0]))
    for forbidden in ("CardFirstSix", "CardLastFour", "220024", "8808", "VTB", _SENTINEL):
        assert forbidden not in rendered
    assert "bearer" not in rendered.lower()
    fields = _rendered(recs[0])
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
    assert fields["resolvedVia"] == "user_id"


# ============================ _level_for table (ADR-054) ============================


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("user_not_found", logging.WARNING),
        ("no_creditable_payment", logging.WARNING),
        ("empty_body", logging.DEBUG),
        ("invalid_json", logging.INFO),
        ("not_an_object", logging.INFO),
        ("not_a_completed_payment", logging.INFO),
        ("invalid_account_id", logging.INFO),
    ],
)
def test_level_for_ignored(reason: str, expected: int) -> None:
    assert _level_for("ignored", reason) == expected


@pytest.mark.parametrize("result", ["applied", "duplicate"])
def test_level_for_applied_duplicate_info(result: str) -> None:
    assert _level_for(result, None) == logging.INFO


# ============================ Migration 0014 — single head + UNIQUE transaction_id ============


def test_alembic_single_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    # Exactly ONE head (a second head would be a branched/broken chain — a real defect).
    assert len(heads) == 1, heads


@pytest.mark.asyncio
async def test_migration_0014_table_and_unique_transaction_id(db_session: AsyncSession) -> None:
    # PRIMARY KEY on transaction_id (== UNIQUE) backs the ON CONFLICT dedup (ADR-054 §3: the column
    # is repurposed to hold the broadapps payment_id — same DDL).
    pk_cols = (
        (
            await db_session.execute(
                text(
                    "SELECT kcu.column_name "
                    "FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "  ON tc.constraint_name = kcu.constraint_name "
                    "WHERE tc.table_name = 'cloudpayments_webhook_events' "
                    "  AND tc.constraint_type = 'PRIMARY KEY'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert list(pk_cols) == ["transaction_id"]
