"""Integration: POST /v1/billing/adapty/webhook (ADR-029, billing-adapty/02,03,07).

Drives the FULL HTTP path through the real per-route bearer auth, the defensive parser, the
single-transaction service (ON CONFLICT dedup + subscription upsert + Wallet.grant + audit) against
the REAL PostgreSQL container. Hermetic: the DB is the shared testcontainers Postgres; the webhook
secret and product-tier map are injected via env + ``get_settings.cache_clear()`` and restored
afterwards (the auth dependency and the service read the lru-cached ``get_settings()``).

Contract invariants under test (02-api-contracts.md):
- 401 on missing/wrong bearer; 500 on unset secret (NOT 401).
- After auth EVERY malformed/unknown payload -> 200 ``ignored/*`` (Adapty's verification ping and
  any garbage body must never produce 5xx, which Adapty would retry forever).
- started/renewed -> applied + subscription active + credit grant by tier.
- cancelled/expired -> applied + subscription expired, balance unchanged.
- replay of the same event_id -> duplicate, no side effects.
- the bearer secret never appears in the response / audit payload.
"""

from __future__ import annotations

import json
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
_PRODUCT_TOKENS = '{"pro_monthly": 5000, "pro_yearly": 12000}'
_GRANT_FALLBACK = 1000


def _auth(secret: str = _SECRET) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
async def adapty_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """ASGI client wired to the container DB with ADAPTY_* env configured (secret + tier map).

    Resets get_settings cache around the test so the Adapty posture never leaks into the
    session-shared settings cache used by the rest of the suite.
    """
    from app import deps
    from app.api_gateway import rate_limit
    from app.main import create_app

    monkeypatch.setenv("ADAPTY_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setenv("ADAPTY_PRODUCT_TOKENS", _PRODUCT_TOKENS)
    monkeypatch.setenv("ADAPTY_SUBSCRIPTION_TOKENS_GRANT", str(_GRANT_FALLBACK))
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


@pytest.fixture
async def adapty_client_no_secret(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """Same wiring but with ADAPTY_WEBHOOK_SECRET unset (blank) -> 500 misconfiguration branch."""
    from app import deps
    from app.api_gateway import rate_limit
    from app.main import create_app

    monkeypatch.setenv("ADAPTY_WEBHOOK_SECRET", "")
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


_URL = "/v1/billing/adapty/webhook"


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


async def _ledger_count(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u"), {"u": str(uid)}
        )
    return int(n)


async def _audit_rows(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID
) -> list[dict[str, Any]]:
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT event_type, payload FROM audit_logs WHERE user_id=:u "
                    "AND event_type='adapty_subscription'"
                ),
                {"u": str(uid)},
            )
        ).all()
    return [{"event_type": r.event_type, "payload": r.payload} for r in rows]


async def _event_count(maker: async_sessionmaker[AsyncSession], event_id: str) -> int:
    async with maker() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM adapty_webhook_events WHERE event_id=:e"), {"e": event_id}
        )
    return int(n)


def _payload(
    *,
    event_id: str,
    event_type: str,
    user_id: uuid.UUID | str,
    vendor_product_id: str | None = "pro_monthly",
    expires_at: str | None = "2026-07-12T00:00:00Z",
) -> dict[str, Any]:
    props: dict[str, Any] = {}
    if vendor_product_id is not None:
        props["vendor_product_id"] = vendor_product_id
    if expires_at is not None:
        props["expires_at"] = expires_at
    return {
        "event_id": event_id,
        "event_type": event_type,
        "customer_user_id": str(user_id),
        "event_properties": props,
    }


# ============================ Authorization ============================


@pytest.mark.asyncio
async def test_no_bearer_returns_401(adapty_client: AsyncClient) -> None:
    r = await adapty_client.post(_URL, content=b"{}")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_wrong_bearer_returns_401(adapty_client: AsyncClient) -> None:
    r = await adapty_client.post(_URL, content=b"{}", headers=_auth("wrong-secret"))
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_unset_secret_returns_500_not_401(adapty_client_no_secret: AsyncClient) -> None:
    # Even WITH a (any) bearer presented, an unset server secret must be a 500 misconfiguration,
    # never a 401: the contract wants Adapty to retry until the operator sets the secret.
    r = await adapty_client_no_secret.post(_URL, content=b"{}", headers=_auth("anything"))
    assert r.status_code == 500, r.text
    # Clear, secret-free misconfiguration text.
    assert "secret" in r.text.lower()


@pytest.mark.asyncio
async def test_unset_secret_returns_500_even_without_bearer(
    adapty_client_no_secret: AsyncClient,
) -> None:
    r = await adapty_client_no_secret.post(_URL, content=b"{}")
    assert r.status_code == 500, r.text


@pytest.mark.asyncio
async def test_valid_bearer_proceeds_to_body_handling(adapty_client: AsyncClient) -> None:
    # Correct secret + empty body -> auth passed, body handled -> 200 ignored/empty_body.
    r = await adapty_client.post(_URL, content=b"", headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "ignored", "reason": "empty_body"}


# ============================ Body shape -> always 200 after auth ============================


@pytest.mark.asyncio
async def test_empty_body_ping_returns_200_ignored(adapty_client: AsyncClient) -> None:
    r = await adapty_client.post(_URL, content=b"", headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "ignored", "reason": "empty_body"}


@pytest.mark.asyncio
async def test_non_json_body_returns_200_invalid_json(adapty_client: AsyncClient) -> None:
    r = await adapty_client.post(_URL, content=b"not json at all <<<", headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "ignored", "reason": "invalid_json"}


@pytest.mark.asyncio
async def test_json_not_an_object_returns_200_not_an_object(adapty_client: AsyncClient) -> None:
    r = await adapty_client.post(_URL, content=b"[1, 2, 3]", headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "ignored", "reason": "not_an_object"}


@pytest.mark.asyncio
async def test_missing_event_id_returns_200_ignored(adapty_client: AsyncClient) -> None:
    body = {"event_type": "subscription_started", "customer_user_id": str(uuid.uuid4())}
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "ignored", "reason": "missing_event_id"}


@pytest.mark.asyncio
async def test_missing_customer_user_id_returns_200_ignored(adapty_client: AsyncClient) -> None:
    body = {"event_id": "evt-x", "event_type": "subscription_started"}
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "ignored", "reason": "missing_customer_user_id"}


@pytest.mark.asyncio
async def test_non_uuid_customer_user_id_returns_200_ignored(adapty_client: AsyncClient) -> None:
    body = {
        "event_id": "evt-x",
        "event_type": "subscription_started",
        "customer_user_id": "not-a-uuid",
    }
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "ignored", "reason": "missing_customer_user_id"}


@pytest.mark.asyncio
async def test_user_not_found_returns_200_ignored(adapty_client: AsyncClient) -> None:
    # Well-formed UUID but no users row: the webhook NEVER provisions a user.
    body = _payload(event_id="evt-nouser", event_type="subscription_started", user_id=uuid.uuid4())
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "ignored", "reason": "user_not_found"}


@pytest.mark.asyncio
async def test_unknown_event_type_returns_200_with_echo(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    body = _payload(event_id="evt-unknown", event_type="subscription_paused", user_id=uid)
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    # echoes the normalised event_type, no reason; no mutation.
    assert r.json() == {"result": "ignored", "event_type": "subscription_paused"}
    assert await _ledger_count(db_sessionmaker, uid) == 0
    assert await _subscription(db_sessionmaker, uid) is None
    assert await _audit_rows(db_sessionmaker, uid) == []


# ============================ Events: started / renewed (grant) ============================


@pytest.mark.asyncio
async def test_subscription_started_applies_and_grants_mapped_tier(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=200)  # pre-existing balance
    body = _payload(
        event_id="evt-started-1",
        event_type="subscription_started",
        user_id=uid,
        vendor_product_id="pro_monthly",  # mapped -> 5000
    )
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "applied"}

    assert await _subscription(db_sessionmaker, uid) == ("active", "pro_monthly")
    # balance += mapped tier (200 + 5000).
    assert await _balance(db_sessionmaker, uid) == 5200
    assert await _ledger_count(db_sessionmaker, uid) == 1


@pytest.mark.asyncio
async def test_subscription_started_grant_uses_exact_idempotency_key_and_meta(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    body = _payload(
        event_id="evt-key-1",
        event_type="subscription_started",
        user_id=uid,
        vendor_product_id="pro_yearly",  # mapped -> 12000
    )
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    async with db_sessionmaker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT type, amount, meta, idempotency_key FROM ledger_transactions "
                    "WHERE user_id=:u"
                ),
                {"u": str(uid)},
            )
        ).one()
    assert row.type == "credit"
    assert int(row.amount) == 12000
    # ADR-047: grant idempotency key is adapty-txn:{txn}, txn = transaction_id ‖
    # original_transaction_id ‖ event_id. This payload carries no transaction id -> txn falls back
    # to the event_id ("evt-key-1").
    assert row.idempotency_key == "adapty-txn:evt-key-1"
    # The grant meta carries the per-period transaction id + event provenance (the wallet records
    # `reason` in the billing_credit audit row, not in the ledger meta).
    assert row.meta["transactionId"] == "evt-key-1"
    assert row.meta["eventType"] == "subscription_started"
    assert row.meta["vendorProductId"] == "pro_yearly"


@pytest.mark.asyncio
async def test_subscription_renewed_applies_and_grants(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        # already an active subscriber renewing.
        uid = await seed_user(s, subscription="active", balance=100)
    body = _payload(
        event_id="evt-renew-1",
        event_type="subscription_renewed",
        user_id=uid,
        vendor_product_id="pro_monthly",
    )
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "applied"}
    assert await _subscription(db_sessionmaker, uid) == ("active", "pro_monthly")
    assert await _balance(db_sessionmaker, uid) == 5100  # 100 + 5000


@pytest.mark.asyncio
async def test_unmapped_product_falls_back_to_grant(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    body = _payload(
        event_id="evt-fallback-1",
        event_type="subscription_started",
        user_id=uid,
        vendor_product_id="pro_unknown_sku",  # not in map -> fallback 1000
    )
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    assert await _balance(db_sessionmaker, uid) == _GRANT_FALLBACK
    assert await _subscription(db_sessionmaker, uid) == ("active", "pro_unknown_sku")


# ============================ Events: cancelled / expired (no grant) ============================


@pytest.mark.asyncio
async def test_subscription_cancelled_marks_expired_balance_unchanged(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=300)
    body = _payload(
        event_id="evt-cancel-1",
        event_type="subscription_cancelled",
        user_id=uid,
        vendor_product_id=None,
        expires_at=None,
    )
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "applied"}
    sub = await _subscription(db_sessionmaker, uid)
    assert sub is not None and sub[0] == "expired"
    # No grant on cancellation: balance unchanged, no new ledger row.
    assert await _balance(db_sessionmaker, uid) == 300
    assert await _ledger_count(db_sessionmaker, uid) == 0


@pytest.mark.asyncio
async def test_subscription_expired_marks_expired(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=42)
    body = _payload(
        event_id="evt-exp-1",
        event_type="subscription_expired",
        user_id=uid,
        vendor_product_id=None,
        expires_at=None,
    )
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "applied"}
    sub = await _subscription(db_sessionmaker, uid)
    assert sub is not None and sub[0] == "expired"
    assert await _balance(db_sessionmaker, uid) == 42
    assert await _ledger_count(db_sessionmaker, uid) == 0


@pytest.mark.asyncio
async def test_expired_for_user_without_subscription_row_creates_expired_row(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)  # no subscription row
    body = _payload(
        event_id="evt-exp-norow",
        event_type="subscription_expired",
        user_id=uid,
        vendor_product_id=None,
        expires_at=None,
    )
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    assert await _subscription(db_sessionmaker, uid) == ("expired", None)


# ============================ Idempotency / resilience (CRITICAL) ============================


@pytest.mark.asyncio
async def test_duplicate_event_id_returns_duplicate_no_side_effects(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=0)
    body = _payload(
        event_id="evt-dup-1",
        event_type="subscription_started",
        user_id=uid,
        vendor_product_id="pro_monthly",
    )
    raw = json.dumps(body).encode()

    r1 = await adapty_client.post(_URL, content=raw, headers=_auth())
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"result": "applied"}
    assert await _balance(db_sessionmaker, uid) == 5000
    assert await _ledger_count(db_sessionmaker, uid) == 1

    # Replay the IDENTICAL event_id: duplicate, no second grant, no subscription rewrite.
    r2 = await adapty_client.post(_URL, content=raw, headers=_auth())
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"result": "duplicate"}

    # Balance unchanged, exactly one ledger row, exactly one webhook-event row.
    assert await _balance(db_sessionmaker, uid) == 5000
    assert await _ledger_count(db_sessionmaker, uid) == 1
    assert await _event_count(db_sessionmaker, "evt-dup-1") == 1
    # Audit recorded only on the applied path, not on the duplicate.
    assert len(await _audit_rows(db_sessionmaker, uid)) == 1


@pytest.mark.asyncio
async def test_duplicate_with_different_body_still_no_second_grant(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Same event_id, DIFFERENT product tier on replay: dedup is purely on event_id, the second
    # delivery must NOT grant the (larger) tier again.
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    first = _payload(
        event_id="evt-dup-2",
        event_type="subscription_started",
        user_id=uid,
        vendor_product_id="pro_monthly",  # 5000
    )
    second = _payload(
        event_id="evt-dup-2",
        event_type="subscription_started",
        user_id=uid,
        vendor_product_id="pro_yearly",  # 12000 — must NOT be granted
    )
    r1 = await adapty_client.post(_URL, content=json.dumps(first).encode(), headers=_auth())
    assert r1.json() == {"result": "applied"}
    r2 = await adapty_client.post(_URL, content=json.dumps(second).encode(), headers=_auth())
    assert r2.json() == {"result": "duplicate"}
    assert await _balance(db_sessionmaker, uid) == 5000  # only the first grant
    assert await _ledger_count(db_sessionmaker, uid) == 1


# ==================== Parsing: alt field names & expires_at over HTTP ====================


@pytest.mark.asyncio
async def test_applies_via_alternative_field_names(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    # id (not event_id), profile.customer_user_id (not top-level), event_properties.product_id
    # (not vendor_product_id).
    body = {
        "id": "evt-altnames",
        "event_type": "subscription_started",
        "profile": {"customer_user_id": str(uid), "expires_at": "2026-08-01T00:00:00Z"},
        "event_properties": {"product_id": "pro_monthly"},
    }
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "applied"}
    assert await _subscription(db_sessionmaker, uid) == ("active", "pro_monthly")
    assert await _balance(db_sessionmaker, uid) == 5000
    assert await _event_count(db_sessionmaker, "evt-altnames") == 1


@pytest.mark.asyncio
async def test_unparseable_expires_at_still_applied(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    body = _payload(
        event_id="evt-badexp",
        event_type="subscription_started",
        user_id=uid,
        vendor_product_id="pro_monthly",
        expires_at="not-a-date",
    )
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "applied"}
    # expires_at stored NULL, but event applied & granted.
    async with db_sessionmaker() as s:
        exp = await s.scalar(
            text("SELECT expires_at FROM subscriptions WHERE user_id=:u"), {"u": str(uid)}
        )
    assert exp is None
    assert await _balance(db_sessionmaker, uid) == 5000


@pytest.mark.asyncio
async def test_expires_at_persisted_on_started(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    body = _payload(
        event_id="evt-exp-persist",
        event_type="subscription_started",
        user_id=uid,
        vendor_product_id="pro_monthly",
        expires_at="2026-09-15T12:00:00Z",
    )
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    async with db_sessionmaker() as s:
        exp = await s.scalar(
            text("SELECT expires_at FROM subscriptions WHERE user_id=:u"), {"u": str(uid)}
        )
    assert exp is not None
    assert exp.year == 2026 and exp.month == 9 and exp.day == 15


# ============= Security: secret never leaks; audit only on applied =============


@pytest.mark.asyncio
async def test_secret_never_in_response_or_audit_payload(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    body = _payload(
        event_id="evt-sec-1",
        event_type="subscription_started",
        user_id=uid,
        vendor_product_id="pro_monthly",
    )
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    # The static bearer secret must not appear in the response body.
    assert _SECRET not in r.text

    rows = await _audit_rows(db_sessionmaker, uid)
    assert len(rows) == 1
    audit_payload = json.dumps(rows[0]["payload"])
    assert _SECRET not in audit_payload
    # Audit carries the adapty event metadata (not the auth secret).
    assert rows[0]["payload"]["adaptyEventId"] == "evt-sec-1"
    assert rows[0]["payload"]["eventType"] == "subscription_started"

    # And the secret must not be in the stored ledger meta either.
    async with db_sessionmaker() as s:
        meta = await s.scalar(
            text("SELECT meta FROM ledger_transactions WHERE user_id=:u"), {"u": str(uid)}
        )
    assert _SECRET not in json.dumps(meta)


@pytest.mark.asyncio
async def test_audit_not_written_on_ignored(
    adapty_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    # unknown event type -> ignored -> no audit row.
    body = _payload(event_id="evt-ign-audit", event_type="subscription_paused", user_id=uid)
    r = await adapty_client.post(_URL, content=json.dumps(body).encode(), headers=_auth())
    assert r.status_code == 200, r.text
    assert await _audit_rows(db_sessionmaker, uid) == []
