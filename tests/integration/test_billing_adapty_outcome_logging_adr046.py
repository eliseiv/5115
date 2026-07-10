"""Integration: structured outcome logging of the Adapty webhook (ADR-046, billing-adapty/08).

Drives ``AdaptyWebhookService.handle()`` DIRECTLY (the logging lives in the service, not the
router) against the REAL PostgreSQL container, capturing the emitted records with ``caplog``.
Hermetic: the DB is the shared testcontainers Postgres (seeded via ``seed_user``); no network, no
real LLM/Adapty calls; ``Settings`` is built inline (no env dependency beyond the conftest
hermetic posture). The bearer secret never reaches the service (it lives in the HTTP header, not
the body), so the "no secret in record" assertions plant a sentinel inside the PAYLOAD and prove
the fixed allowlist drops it.

Invariants under test (ADR-046 §Таблица уровней / 08-observability.md §Тестовые ориентиры):
- EXACTLY ONE ``adapty_webhook_outcome`` record per ``handle()`` call — no double, no miss.
- Correct ``result``/``reason`` and log LEVEL per outcome.
- ``eventId``/``customerUserId`` present where parsed, ABSENT AS A KEY (not ``null``) on early
  reasons — verified through the real ``JsonFormatter`` (which drops ``None`` keys).
- ``customerUserId`` serialises as a UUID STRING, not an object.
- Neither the raw payload nor any Authorization/bearer secret appears in the record.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.billing_adapty.service import AdaptyWebhookService
from app.config import Settings
from app.observability.logging import JsonFormatter
from app.wallet.service import WalletService
from tests.conftest import seed_user

_MESSAGE = "adapty_webhook_outcome"
# A value planted inside the webhook body; it must NEVER surface in the outcome record (the
# allowlist is result/reason/eventType/eventId/customerUserId only).
_SENTINEL_SECRET = "Bearer super-secret-adapty-token-DO-NOT-LOG"  # noqa: S105 - test sentinel


def _settings() -> Settings:
    # pro_monthly -> 5000 so the applied/duplicate path can grant without error; the values are
    # irrelevant to the logging assertions (grant amount is not logged).
    return Settings(
        ADAPTY_WEBHOOK_SECRET="x",  # noqa: S106 - test-only static secret
        ADAPTY_PRODUCT_TOKENS='{"pro_monthly": 5000}',
        ADAPTY_SUBSCRIPTION_TOKENS_GRANT=1000,
    )


@pytest.fixture
async def adapty_service(db_session: AsyncSession) -> AsyncIterator[AdaptyWebhookService]:
    """A service bound to the container DB session (real Wallet/Audit, inline Settings).

    Re-enable the service logger: the in-process Alembic migration (``fileConfig`` with the default
    ``disable_existing_loggers=True``) DISABLES every logger created before it ran — including
    ``app.billing_adapty.service`` (created at module import) — which would silently swallow these
    records under ``caplog``. This is a TEST-HARNESS artifact only: in production, migrations run in
    a separate process and the API configures logging via ``configure_logging`` (which never
    disables loggers), so the production logger is never disabled.
    """
    logging.getLogger("app.billing_adapty.service").disabled = False
    audit = AuditService(db_session)
    yield AdaptyWebhookService(db_session, WalletService(db_session, audit), audit, _settings())


def _payload(
    *,
    event_id: str | None,
    event_type: str,
    user_id: uuid.UUID | str | None,
    with_secret: bool = False,
) -> bytes:
    body: dict[str, Any] = {"event_type": event_type}
    if event_id is not None:
        body["event_id"] = event_id
    if user_id is not None:
        body["customer_user_id"] = str(user_id)
    body["event_properties"] = {
        "vendor_product_id": "pro_monthly",
        "expires_at": "2026-07-12T00:00:00Z",
    }
    if with_secret:
        # Sentinel secret-like fields planted in the body: the allowlist must drop them entirely.
        body["authorization"] = _SENTINEL_SECRET
        body["event_properties"]["vendor_product_id"] = _SENTINEL_SECRET
    return json.dumps(body).encode()


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == _MESSAGE]


def _rendered(record: logging.LogRecord) -> dict[str, Any]:
    """Render a record through the REAL JsonFormatter (drops ``None`` keys) and parse it back.

    This is how "absent as a key, not null" is verified: the formatter — not the service — is
    what strips ``None`` fields from the final JSON line operators see.
    """
    return json.loads(JsonFormatter().format(record))


# ============================ Early reasons (no DB) ============================
# raw/JSON-shape outcomes: eventId/customerUserId/eventType must ALL be absent keys.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw, reason, level",
    [
        (b"", "empty_body", logging.DEBUG),
        (b"not json at all <<<", "invalid_json", logging.INFO),
        (b"[1, 2, 3]", "not_an_object", logging.INFO),
    ],
)
async def test_body_shape_outcomes_level_and_no_parsed_keys(
    adapty_service: AdaptyWebhookService,
    caplog: pytest.LogCaptureFixture,
    raw: bytes,
    reason: str,
    level: int,
) -> None:
    caplog.set_level(logging.DEBUG)  # DEBUG so empty_body (DEBUG) is captured too.
    outcome = await adapty_service.handle(raw)

    assert outcome.result == "ignored"
    assert outcome.reason == reason
    recs = _outcomes(caplog)
    assert len(recs) == 1, "exactly one outcome record per handle()"
    rec = recs[0]
    assert rec.levelno == level
    assert rec.getMessage() == _MESSAGE
    fields = _rendered(rec)
    assert fields["result"] == "ignored"
    assert fields["reason"] == reason
    # Nothing parsed yet -> these keys must be ABSENT (not null).
    assert "eventId" not in fields
    assert "customerUserId" not in fields
    assert "eventType" not in fields


@pytest.mark.asyncio
async def test_missing_event_id_info_no_parsed_keys(
    adapty_service: AdaptyWebhookService, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    raw = json.dumps({"event_type": "subscription_started"}).encode()
    outcome = await adapty_service.handle(raw)

    assert (outcome.result, outcome.reason) == ("ignored", "missing_event_id")
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.INFO
    fields = _rendered(recs[0])
    assert fields["reason"] == "missing_event_id"
    assert "eventId" not in fields
    assert "customerUserId" not in fields
    assert "eventType" not in fields


@pytest.mark.asyncio
async def test_missing_customer_user_id_warning_eventid_only(
    adapty_service: AdaptyWebhookService, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    raw = json.dumps({"event_id": "evt-no-user", "event_type": "subscription_started"}).encode()
    outcome = await adapty_service.handle(raw)

    assert (outcome.result, outcome.reason) == ("ignored", "missing_customer_user_id")
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    fields = _rendered(recs[0])
    assert fields["reason"] == "missing_customer_user_id"
    # ADR-047 (ADR-046 synergy): event_type is parsed BEFORE the customer_user_id check and carried
    # into this branch, so the operator sees "subscription_started arrived but no customer_user_id".
    # eventId + eventType are present here; customerUserId is NOT (no user resolved).
    assert fields["eventId"] == "evt-no-user"
    assert fields["eventType"] == "subscription_started"
    assert "customerUserId" not in fields


# ============================ DB-backed reasons ============================


@pytest.mark.asyncio
async def test_user_not_found_warning_all_context(
    adapty_service: AdaptyWebhookService, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    # Well-formed UUID with no users row -> potentially lost grant -> WARNING.
    uid = uuid.uuid4()
    raw = _payload(event_id="evt-nouser", event_type="subscription_started", user_id=uid)
    outcome = await adapty_service.handle(raw)

    assert (outcome.result, outcome.reason) == ("ignored", "user_not_found")
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    fields = _rendered(recs[0])
    assert fields["reason"] == "user_not_found"
    assert fields["eventId"] == "evt-nouser"
    assert fields["eventType"] == "subscription_started"
    # customerUserId present AND serialised as a UUID STRING (not an object).
    assert fields["customerUserId"] == str(uid)
    assert isinstance(fields["customerUserId"], str)


@pytest.mark.asyncio
async def test_unknown_event_type_warning_reason_absent_echoes_type(
    adapty_service: AdaptyWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    caplog.set_level(logging.DEBUG)
    raw = _payload(event_id="evt-unknown", event_type="subscription_paused", user_id=uid)
    outcome = await adapty_service.handle(raw)

    # The only ignored outcome with reason=None: the unknown-event_type echo -> WARNING.
    assert outcome.result == "ignored"
    assert outcome.reason is None
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    fields = _rendered(recs[0])
    assert fields["result"] == "ignored"
    # reason is None -> dropped by the formatter: ABSENT, not null.
    assert "reason" not in fields
    assert fields["eventType"] == "subscription_paused"  # echoed
    assert fields["eventId"] == "evt-unknown"
    assert fields["customerUserId"] == str(uid)


@pytest.mark.asyncio
async def test_applied_info_full_context_no_reason(
    adapty_service: AdaptyWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    caplog.set_level(logging.DEBUG)
    raw = _payload(event_id="evt-applied", event_type="subscription_started", user_id=uid)
    outcome = await adapty_service.handle(raw)
    await adapty_service._session.commit()

    assert outcome.result == "applied"
    recs = _outcomes(caplog)
    assert len(recs) == 1
    assert recs[0].levelno == logging.INFO
    fields = _rendered(recs[0])
    assert fields["result"] == "applied"
    assert "reason" not in fields  # applied carries reason=None
    assert fields["eventId"] == "evt-applied"
    assert fields["eventType"] == "subscription_started"
    assert fields["customerUserId"] == str(uid)


@pytest.mark.asyncio
async def test_duplicate_info_and_one_record_per_call(
    adapty_service: AdaptyWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Replay of one event_id: applied then duplicate — each emits EXACTLY one record."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    raw = _payload(event_id="evt-dup", event_type="subscription_started", user_id=uid)

    caplog.set_level(logging.DEBUG)
    first = await adapty_service.handle(raw)
    await adapty_service._session.commit()
    assert first.result == "applied"
    assert len(_outcomes(caplog)) == 1  # exactly one for the first call

    caplog.clear()
    second = await adapty_service.handle(raw)
    await adapty_service._session.commit()
    assert second.result == "duplicate"
    recs = _outcomes(caplog)
    assert len(recs) == 1  # exactly one for the replay — no double, no miss
    assert recs[0].levelno == logging.INFO
    fields = _rendered(recs[0])
    assert fields["result"] == "duplicate"
    assert "reason" not in fields
    assert fields["eventId"] == "evt-dup"
    assert fields["eventType"] == "subscription_started"
    assert fields["customerUserId"] == str(uid)


# ==================== Security: allowlist drops payload + secret ====================


@pytest.mark.asyncio
async def test_record_contains_no_raw_payload_or_bearer_secret(
    adapty_service: AdaptyWebhookService,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    caplog.set_level(logging.DEBUG)
    # Plant a bearer-secret sentinel + extra payload fields in the body.
    raw = _payload(
        event_id="evt-sec", event_type="subscription_started", user_id=uid, with_secret=True
    )
    await adapty_service.handle(raw)
    await adapty_service._session.commit()

    recs = _outcomes(caplog)
    assert len(recs) == 1
    rendered = json.dumps(_rendered(recs[0]))
    # The planted secret never appears; neither does the raw vendor_product_id / expires_at.
    assert _SENTINEL_SECRET not in rendered
    assert "vendor_product_id" not in rendered
    assert "expires_at" not in rendered
    assert "authorization" not in rendered.lower()
    assert "bearer" not in rendered.lower()
    # Only the fixed allowlist of keys is present.
    fields = _rendered(recs[0])
    assert set(fields) <= {
        "level",
        "logger",
        "message",
        "result",
        "reason",
        "eventType",
        "eventId",
        "customerUserId",
        "requestId",
        "sessionId",
        "userId",
    }


# ============================ Level table (compact, all outcomes) ============================


@pytest.mark.parametrize(
    "reason, expected_level",
    [
        ("user_not_found", logging.WARNING),
        ("missing_customer_user_id", logging.WARNING),
        (None, logging.WARNING),  # unknown event_type echo
        ("empty_body", logging.DEBUG),
        ("invalid_json", logging.INFO),
        ("not_an_object", logging.INFO),
        ("missing_event_id", logging.INFO),
    ],
)
def test_level_for_ignored_outcomes(reason: str | None, expected_level: int) -> None:
    from app.billing_adapty.service import _level_for

    assert _level_for("ignored", reason) == expected_level


@pytest.mark.parametrize("result", ["applied", "duplicate"])
def test_level_for_applied_and_duplicate_is_info(result: str) -> None:
    from app.billing_adapty.service import _level_for

    assert _level_for(result, None) == logging.INFO
