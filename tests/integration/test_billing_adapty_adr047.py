"""Integration: ADR-047 — real Adapty payload format + per-period grant idempotency.

Drives the FULL HTTP path (``POST /v1/billing/adapty/webhook``) through the real per-route bearer
auth, the reworked defensive parser and the single-transaction service against the REAL PostgreSQL
container. Hermetic: the DB is the shared testcontainers Postgres; the webhook secret and product
map are injected via env + ``get_settings.cache_clear()`` and restored afterwards; no network to
Adapty; no LLM. Placeholder/empty LLM keys are irrelevant (the Adapty path never touches an LLM
client).

Covers the ADR-047 §8.4 test oriented invariants:
- one real purchase = three events (trial_started + access_level_updated(premium,active) +
  trial_renewal_cancelled) sharing ONE transaction_id but distinct profile_event_id -> EXACTLY ONE
  ledger grant (key ``adapty-txn:{transaction_id}``), subscription active, expires_at from
  ``subscription_expires_at``;
- ``profile_event_id`` (incl. a bare int) is the event id; the real payload no longer 200/ignores
  on ``missing_event_id``;
- ``*_renewal_cancelled`` -> NOOP (subscription + balance untouched, event recorded, audit
  ``semantics=noop``, result ``applied``);
- ``access_level_updated`` is_active=false -> EXPIRING (subscription expired, balance untouched);
- grant idempotency across distinct profile_event_id of one txn; replay of the same
  profile_event_id -> duplicate (no mutations);
- renewal (subscription_renewed, NEW transaction_id, same original_transaction_id) -> a NEW grant
  (does NOT collapse into the first period);
- missing customer_user_id (only profile_id) -> ``missing_customer_user_id`` (NOT
  ``missing_event_id``) and the structured log carries the event type;
- tier from ``ADAPTY_PRODUCT_TOKENS`` (exact) vs fallback for an unmapped product.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import seed_user

_SECRET = "adapty-webhook-secret-value"  # noqa: S105 - test-only static secret
_WEEK_PRODUCT = "week_6.99_nottrial"
_WEEK_TOKENS = 700  # mapped tier for week_6.99_nottrial (distinct from the fallback)
_FALLBACK = 1000
_PRODUCT_TOKENS = json.dumps({_WEEK_PRODUCT: _WEEK_TOKENS})
# The real transaction id from the ADR-047 example (arrives as a bare int -> coerced to str).
_TXN = 410003298316682
_EXPIRES = "2026-07-07T00:00:00Z"

_URL = "/v1/billing/adapty/webhook"


def _auth(secret: str = _SECRET) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
async def adapty_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """ASGI client wired to the container DB with ADAPTY_* env configured (secret + tier map)."""
    from app import deps
    from app.api_gateway import rate_limit
    from app.main import create_app

    monkeypatch.setenv("ADAPTY_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setenv("ADAPTY_PRODUCT_TOKENS", _PRODUCT_TOKENS)
    monkeypatch.setenv("ADAPTY_SUBSCRIPTION_TOKENS_GRANT", str(_FALLBACK))
    get_settings.cache_clear()

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _allow(**_kwargs: Any) -> bool:
        return True

    orig_other = rate_limit.enforce_other_limits
    rate_limit.enforce_other_limits = _allow  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    rate_limit.enforce_other_limits = orig_other  # type: ignore[assignment]
    get_settings.cache_clear()


# --------------------------- payload builders (real Adapty wire form) ---------------------------


def _event(
    *,
    profile_event_id: str | int,
    event_type: str,
    user_id: uuid.UUID | str | None,
    transaction_id: str | int | None = _TXN,
    original_transaction_id: str | int | None = None,
    vendor_product_id: str | None = _WEEK_PRODUCT,
    subscription_expires_at: str | None = _EXPIRES,
    is_active: Any = None,
    access_level_id: str | None = None,
    will_renew: Any = None,
    include_customer_user_id: bool = True,
    profile_id: str | None = None,
) -> bytes:
    """Build one Adapty event in the REAL wire shape: business fields under ``event_properties``.

    ``profile_event_id`` is the per-event id; ``customer_user_id`` is included by default (post
    ``Adapty.identify`` state). Set ``include_customer_user_id=False`` to model the pre-identify
    payload that carries only Adapty's ``profile_id``.
    """
    ep: dict[str, Any] = {}
    if transaction_id is not None:
        ep["transaction_id"] = transaction_id
    if original_transaction_id is not None:
        ep["original_transaction_id"] = original_transaction_id
    if vendor_product_id is not None:
        ep["vendor_product_id"] = vendor_product_id
    if subscription_expires_at is not None:
        ep["subscription_expires_at"] = subscription_expires_at
    if is_active is not None:
        ep["is_active"] = is_active
    if access_level_id is not None:
        ep["access_level_id"] = access_level_id
    if will_renew is not None:
        ep["will_renew"] = will_renew
    body: dict[str, Any] = {
        "profile_event_id": profile_event_id,
        "event_type": event_type,
        "event_properties": ep,
    }
    if include_customer_user_id and user_id is not None:
        body["customer_user_id"] = str(user_id)
    if profile_id is not None:
        body["profile_id"] = profile_id
    return json.dumps(body).encode()


async def _post(client: AsyncClient, body: bytes) -> Any:
    r = await client.post(_URL, content=body, headers=_auth())
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------- DB helpers ---------------------------


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


async def _sub_expires(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> Any:
    async with maker() as s:
        return await s.scalar(
            text("SELECT expires_at FROM subscriptions WHERE user_id=:u"), {"u": str(uid)}
        )


async def _ledger_rows(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID
) -> list[dict[str, Any]]:
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT type, amount, idempotency_key, meta FROM ledger_transactions "
                    "WHERE user_id=:u ORDER BY created_at"
                ),
                {"u": str(uid)},
            )
        ).all()
    return [
        {
            "type": r.type,
            "amount": int(r.amount),
            "idempotency_key": r.idempotency_key,
            "meta": r.meta,
        }
        for r in rows
    ]


async def _audit_rows(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID
) -> list[dict[str, Any]]:
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT payload FROM audit_logs WHERE user_id=:u "
                    "AND event_type='adapty_subscription' ORDER BY created_at"
                ),
                {"u": str(uid)},
            )
        ).all()
    return [dict(r.payload) for r in rows]


async def _event_count(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM adapty_webhook_events WHERE user_id=:u"), {"u": str(uid)}
        )
    return int(n)


# ============================ 1. Real purchase: 3 events, ONE grant ============================


@pytest.mark.asyncio
async def test_real_purchase_three_events_grant_once(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)  # no balance row yet, no subscription
    # The three events Adapty emits for ONE weekly trial purchase: distinct profile_event_id,
    # ONE shared transaction_id, same vendor_product_id.
    e_trial = _event(
        profile_event_id="a3254174-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        event_type="trial_started",
        user_id=uid,
    )
    e_access = _event(
        profile_event_id="815af018-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        event_type="access_level_updated",
        user_id=uid,
        is_active=True,
        access_level_id="premium",
    )
    e_cancel = _event(
        profile_event_id="80d0caf6-cccc-cccc-cccc-cccccccccccc",
        event_type="trial_renewal_cancelled",
        user_id=uid,
        will_renew=False,
    )

    assert await _post(adapty_client, e_trial) == {"result": "applied"}
    assert await _post(adapty_client, e_access) == {"result": "applied"}
    assert await _post(adapty_client, e_cancel) == {"result": "applied"}

    # Subscription active, plan = vendor_product_id, expires_at from subscription_expires_at.
    assert await _subscription(db_sessionmaker, uid) == ("active", _WEEK_PRODUCT)
    exp = await _sub_expires(db_sessionmaker, uid)
    assert exp is not None and (exp.year, exp.month, exp.day) == (2026, 7, 7)

    # EXACTLY ONE ledger grant despite two granting-events; keyed by adapty-txn:{transaction_id}.
    ledger = await _ledger_rows(db_sessionmaker, uid)
    assert len(ledger) == 1, ledger
    assert ledger[0]["type"] == "credit"
    assert ledger[0]["amount"] == _WEEK_TOKENS
    assert ledger[0]["idempotency_key"] == f"adapty-txn:{_TXN}"
    assert ledger[0]["meta"]["transactionId"] == str(_TXN)
    # Balance == exactly the tariff (single grant).
    assert await _balance(db_sessionmaker, uid) == _WEEK_TOKENS

    # All three events recorded for delivery-dedup (distinct profile_event_id).
    assert await _event_count(db_sessionmaker, uid) == 3
    # Audit: three rows (granting/granting/noop), the last is the noop cancel.
    audits = await _audit_rows(db_sessionmaker, uid)
    assert [a["semantics"] for a in audits] == ["granting", "granting", "noop"]


@pytest.mark.asyncio
async def test_real_purchase_unmapped_product_falls_back(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Same purchase but the vendor_product_id is NOT in ADAPTY_PRODUCT_TOKENS -> fallback grant.
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    body = _event(
        profile_event_id="evt-unmapped",
        event_type="trial_started",
        user_id=uid,
        vendor_product_id="week_unmapped_sku",
    )
    assert await _post(adapty_client, body) == {"result": "applied"}
    assert await _balance(db_sessionmaker, uid) == _FALLBACK
    assert await _subscription(db_sessionmaker, uid) == ("active", "week_unmapped_sku")


# ============================ 2. profile_event_id is the event id ============================


@pytest.mark.asyncio
async def test_numeric_profile_event_id_is_accepted(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # profile_event_id arrives as a bare int -> coerced to str; event applied (not missing id).
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    body = _event(
        profile_event_id=999000111222,
        event_type="trial_started",
        user_id=uid,
    )
    assert await _post(adapty_client, body) == {"result": "applied"}
    assert await _event_count(db_sessionmaker, uid) == 1


@pytest.mark.asyncio
async def test_real_payload_not_missing_event_id(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A real-shape payload (profile_event_id present, no legacy event_id/id) must NOT 200/ignore
    # on missing_event_id — it reaches the applied path.
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    body = _event(
        profile_event_id="profile-evt-real",
        event_type="subscription_started",
        user_id=uid,
    )
    assert await _post(adapty_client, body) == {"result": "applied"}


# ============================ 3. *_renewal_cancelled -> NOOP ============================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type", ["subscription_renewal_cancelled", "trial_renewal_cancelled"]
)
async def test_renewal_cancelled_is_noop(
    adapty_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    event_type: str,
) -> None:
    # Pre-existing active subscriber with a balance: a renewal-cancel must NOT revoke access nor
    # touch the balance — access is kept until period end.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=250)
    body = _event(
        profile_event_id=f"evt-noop-{event_type}",
        event_type=event_type,
        user_id=uid,
        will_renew=False,
    )
    assert await _post(adapty_client, body) == {"result": "applied"}

    # Subscription unchanged (still active), balance unchanged, no new ledger row.
    sub = await _subscription(db_sessionmaker, uid)
    assert sub is not None and sub[0] == "active"
    assert await _balance(db_sessionmaker, uid) == 250
    assert await _ledger_rows(db_sessionmaker, uid) == []
    # Event recorded for delivery-dedup; audit semantics == noop.
    assert await _event_count(db_sessionmaker, uid) == 1
    audits = await _audit_rows(db_sessionmaker, uid)
    assert len(audits) == 1
    assert audits[0]["semantics"] == "noop"


@pytest.mark.asyncio
async def test_noop_without_subscription_row_keeps_null_state(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # NOOP for a user with no subscription row: nothing created, audit echoes null status.
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    body = _event(
        profile_event_id="evt-noop-norow",
        event_type="subscription_renewal_cancelled",
        user_id=uid,
    )
    assert await _post(adapty_client, body) == {"result": "applied"}
    assert await _subscription(db_sessionmaker, uid) is None
    assert await _ledger_rows(db_sessionmaker, uid) == []
    audits = await _audit_rows(db_sessionmaker, uid)
    assert audits[0]["semantics"] == "noop"
    assert audits[0]["status"] is None


# ============================ 4. access_level_updated is_active=false -> EXPIRING ============


@pytest.mark.asyncio
async def test_access_level_updated_inactive_expires(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=321)
    body = _event(
        profile_event_id="evt-access-off",
        event_type="access_level_updated",
        user_id=uid,
        is_active=False,
    )
    assert await _post(adapty_client, body) == {"result": "applied"}
    sub = await _subscription(db_sessionmaker, uid)
    assert sub is not None and sub[0] == "expired"
    # Balance untouched on expiry.
    assert await _balance(db_sessionmaker, uid) == 321
    assert await _ledger_rows(db_sessionmaker, uid) == []
    audits = await _audit_rows(db_sessionmaker, uid)
    assert audits[0]["semantics"] == "expiring"


# ============================ 5. Grant idempotency / duplicate dedup ============================


@pytest.mark.asyncio
async def test_two_granting_events_one_txn_grant_once(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Two DIFFERENT granting events (distinct profile_event_id) sharing ONE transaction_id -> a
    # single grant (the second passes event-dedup but hits the same adapty-txn idempotency key).
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    first = _event(
        profile_event_id="evt-grant-a",
        event_type="trial_started",
        user_id=uid,
    )
    second = _event(
        profile_event_id="evt-grant-b",
        event_type="access_level_updated",
        user_id=uid,
        is_active=True,
        access_level_id="premium",
    )
    assert await _post(adapty_client, first) == {"result": "applied"}
    assert await _post(adapty_client, second) == {"result": "applied"}

    ledger = await _ledger_rows(db_sessionmaker, uid)
    assert len(ledger) == 1
    assert ledger[0]["amount"] == _WEEK_TOKENS
    assert await _balance(db_sessionmaker, uid) == _WEEK_TOKENS
    # Both events recorded (distinct profile_event_id).
    assert await _event_count(db_sessionmaker, uid) == 2


@pytest.mark.asyncio
async def test_replay_same_profile_event_id_is_duplicate(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Re-delivery of the SAME profile_event_id -> duplicate, no second grant, no extra event row.
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    body = _event(
        profile_event_id="evt-replay",
        event_type="trial_started",
        user_id=uid,
    )
    assert await _post(adapty_client, body) == {"result": "applied"}
    assert await _post(adapty_client, body) == {"result": "duplicate"}

    assert len(await _ledger_rows(db_sessionmaker, uid)) == 1
    assert await _balance(db_sessionmaker, uid) == _WEEK_TOKENS
    assert await _event_count(db_sessionmaker, uid) == 1
    # Audit only on the applied path, not the duplicate.
    assert len(await _audit_rows(db_sessionmaker, uid)) == 1


# ==================== 6. Renewal: new transaction_id -> new grant ====================


@pytest.mark.asyncio
async def test_renewal_new_txn_grants_again(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Period 1 (txn A) then a renewal (txn B, SAME original_transaction_id) must NOT collapse: the
    # renewal grants afresh because the grant key is the per-period transaction_id, not the chain.
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    original = 111222333
    period1 = _event(
        profile_event_id="evt-period1",
        event_type="subscription_started",
        user_id=uid,
        transaction_id=1000,
        original_transaction_id=original,
    )
    period2 = _event(
        profile_event_id="evt-period2",
        event_type="subscription_renewed",
        user_id=uid,
        transaction_id=2000,  # NEW per-period txn
        original_transaction_id=original,  # same chain
    )
    assert await _post(adapty_client, period1) == {"result": "applied"}
    assert await _post(adapty_client, period2) == {"result": "applied"}

    ledger = await _ledger_rows(db_sessionmaker, uid)
    assert len(ledger) == 2, ledger
    keys = {row["idempotency_key"] for row in ledger}
    assert keys == {"adapty-txn:1000", "adapty-txn:2000"}
    # Two grants of the mapped tier.
    assert await _balance(db_sessionmaker, uid) == 2 * _WEEK_TOKENS


# ==================== 7. Missing customer_user_id (only profile_id) ====================


@pytest.mark.asyncio
async def test_missing_customer_user_id_with_profile_id(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # The pre-Adapty.identify payload carries only profile_id (no customer_user_id). Now that the
    # event id parses from profile_event_id, the reason is missing_customer_user_id (NOT
    # missing_event_id). No DB mutations.
    r = await adapty_client.post(
        _URL,
        content=_event(
            profile_event_id="evt-no-cuid",
            event_type="trial_started",
            user_id=None,
            include_customer_user_id=False,
            profile_id="3bf27b33-dddd-dddd-dddd-dddddddddddd",
        ),
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "ignored", "reason": "missing_customer_user_id"}


@pytest.mark.asyncio
async def test_missing_customer_user_id_log_carries_event_type(
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The structured outcome log of the missing_customer_user_id branch must carry the parsed
    # event_type (ADR-047/ADR-046 synergy). Drive the service directly to capture the record.
    from app.audit.service import AuditService
    from app.billing_adapty.service import AdaptyWebhookService
    from app.config import Settings
    from app.observability.logging import JsonFormatter
    from app.wallet.service import WalletService

    logging.getLogger("app.billing_adapty.service").disabled = False
    settings = Settings(
        ADAPTY_WEBHOOK_SECRET="x",  # noqa: S106 - test-only static secret
        ADAPTY_PRODUCT_TOKENS=_PRODUCT_TOKENS,
        ADAPTY_SUBSCRIPTION_TOKENS_GRANT=_FALLBACK,
    )
    audit = AuditService(db_session)
    service = AdaptyWebhookService(db_session, WalletService(db_session, audit), audit, settings)

    caplog.set_level(logging.DEBUG)
    raw = _event(
        profile_event_id="evt-log-cuid",
        event_type="trial_started",
        user_id=None,
        include_customer_user_id=False,
    )
    outcome = await service.handle(raw)
    assert (outcome.result, outcome.reason) == ("ignored", "missing_customer_user_id")
    recs = [r for r in caplog.records if r.msg == "adapty_webhook_outcome"]
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    # Render through the real JsonFormatter (drops None keys) — the event type must be carried.
    fields = json.loads(JsonFormatter().format(recs[0]))
    assert fields["eventType"] == "trial_started"
    assert fields["eventId"] == "evt-log-cuid"
    assert fields["reason"] == "missing_customer_user_id"
