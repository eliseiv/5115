"""Integration: admin POST /v1/admin/subscription/grant (ADR-048, admin/09-testing.md).

Real PostgreSQL (testcontainers). Verify-less subscription upsert + optional idempotent credit
grant in ONE transaction: happy paths (expiresAt/days/credits default/0/N), namespace-isolated
ledger key, idempotent replay, 409 with full rollback (no partial apply), 404, validation, root-
cause policy unblock, security (401/413/429), audit without secret. Hermetic: admin secret is set
on settings and admin rate-limit is forced open; no network, no LLM calls (placeholder keys).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, FakeStoreKitVerifier, auth_headers, seed_user

_ADMIN_SECRET = "admin-secret-sub-grant-0123456789abcdef0123456789ab"
_ADMIN_HEADERS = {"X-Admin-Token": _ADMIN_SECRET}
_CREDITS_PER_PERIOD = 1000  # SUBSCRIPTION_CREDITS_PER_PERIOD default (config.py) — asserted below


@pytest.fixture
async def admin_client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_storekit: FakeStoreKitVerifier,
) -> AsyncIterator[AsyncClient]:
    """App client with admin secret configured and admin rate-limit forced open (see test_admin)."""
    settings = get_settings()
    orig_secret = settings.admin_api_secret
    settings.admin_api_secret = _ADMIN_SECRET
    # Guard: the default-credits assertions rely on the documented default of 1000.
    assert settings.subscription_credits_per_period == _CREDITS_PER_PERIOD

    from app import deps
    from app.api_gateway import rate_limit
    from app.api_gateway.routers import admin as admin_router
    from app.chat import anthropic_client as anthropic_mod
    from app.main import create_app
    from app.subscription import storekit as storekit_mod

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    anthropic_mod._anthropic_singleton = fake_anthropic  # type: ignore[assignment]
    storekit_mod._verifier_singleton = fake_storekit  # type: ignore[assignment]

    async def _allow_admin(**_kwargs: Any) -> bool:
        return True

    orig_admin = rate_limit.enforce_admin_limits
    rate_limit.enforce_admin_limits = _allow_admin  # type: ignore[assignment]
    admin_router.enforce_admin_limits = _allow_admin  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    settings.admin_api_secret = orig_secret
    rate_limit.enforce_admin_limits = orig_admin  # type: ignore[assignment]
    admin_router.enforce_admin_limits = orig_admin  # type: ignore[assignment]


# ----------------------------- DB read helpers -----------------------------
async def _balance(maker: async_sessionmaker[AsyncSession], uid: str) -> int:
    async with maker() as s:
        row = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": uid})
        return int(row) if row is not None else 0


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, uid: str) -> int:
    async with maker() as s:
        return int(await s.scalar(text(sql), {"u": uid}) or 0)


async def _subscription(maker: async_sessionmaker[AsyncSession], uid: str) -> dict[str, Any] | None:
    async with maker() as s:
        row = (
            await s.execute(
                text("SELECT status, plan, expires_at FROM subscriptions WHERE user_id=:u"),
                {"u": uid},
            )
        ).first()
    if row is None:
        return None
    return {"status": row[0], "plan": row[1], "expires_at": row[2]}


def _future_iso(days: int = 10) -> str:
    return (datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=days)).isoformat()


# ============================ Happy paths ============================
@pytest.mark.asyncio
async def test_grant_expires_at_default_credits(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=True, subscription=None, balance=0)
    exp = _future_iso(10)
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(uid), "expiresAt": exp, "idempotencyKey": "s-1"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["plan"] == "manual_grant"  # default plan
    assert body["creditsGranted"] == _CREDITS_PER_PERIOD  # credits omitted -> per-period default
    assert body["newBalance"] == _CREDITS_PER_PERIOD
    assert body["ledgerTxId"] is not None
    assert body["idempotentReplay"] is False

    # Subscription row created active with the exact expires_at.
    sub = await _subscription(db_sessionmaker, str(uid))
    assert sub is not None
    assert sub["status"] == "active"
    assert sub["expires_at"] == datetime.datetime.fromisoformat(exp)
    assert await _balance(db_sessionmaker, str(uid)) == _CREDITS_PER_PERIOD


@pytest.mark.asyncio
async def test_grant_days_sets_future_tzaware_expiry(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None, balance=0)
    before = datetime.datetime.now(tz=datetime.UTC)
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(uid), "days": 30, "idempotencyKey": "s-days"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    expires = datetime.datetime.fromisoformat(r.json()["expiresAt"])
    assert expires.tzinfo is not None  # tz-aware
    expected = before + datetime.timedelta(days=30)
    assert abs((expires - expected).total_seconds()) < 60  # ~now()+days

    sub = await _subscription(db_sessionmaker, str(uid))
    assert sub is not None and sub["expires_at"].tzinfo is not None
    assert sub["expires_at"] > datetime.datetime.now(tz=datetime.UTC)


@pytest.mark.asyncio
async def test_grant_credits_zero_activates_without_grant(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None, balance=7)
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(uid), "days": 30, "idempotencyKey": "s-zero", "credits": 0},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["creditsGranted"] == 0
    assert body["newBalance"] is None
    assert body["ledgerTxId"] is None
    assert body["idempotentReplay"] is None

    # No ledger row, balance untouched.
    ledger = await _count(
        db_sessionmaker, "SELECT count(*) FROM ledger_transactions WHERE user_id=:u", str(uid)
    )
    assert ledger == 0
    assert await _balance(db_sessionmaker, str(uid)) == 7

    # audit admin_subscription_grant present with creditsGranted=0 and NO ledgerTxId.
    async with db_sessionmaker() as s:
        payload = await s.scalar(
            text(
                "SELECT payload FROM audit_logs "
                "WHERE user_id=:u AND event_type='admin_subscription_grant'"
            ),
            {"u": str(uid)},
        )
    assert payload is not None
    assert payload["creditsGranted"] == 0
    assert "ledgerTxId" not in payload
    assert payload["actor"] == "admin"


@pytest.mark.asyncio
async def test_grant_explicit_credits_n(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None, balance=0)
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(uid), "days": 30, "idempotencyKey": "s-n", "credits": 42},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["creditsGranted"] == 42
    assert r.json()["newBalance"] == 42
    assert await _balance(db_sessionmaker, str(uid)) == 42


@pytest.mark.asyncio
async def test_grant_upsert_over_existing_subscription(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Pre-existing subscription row (status/plan/expires from seed) -> upsert overwrites it and
    # does NOT create a second row (PK user_id).
    async with db_sessionmaker() as s:
        uid = await seed_user(
            s, subscription="expired", expires_in_hours=-5, balance=0
        )  # seed plan='pro'
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "days": 60,
            "idempotencyKey": "s-upsert",
            "plan": "vip",
            "credits": 0,
        },
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text
    rows = await _count(
        db_sessionmaker, "SELECT count(*) FROM subscriptions WHERE user_id=:u", str(uid)
    )
    assert rows == 1  # upsert, not a second row
    sub = await _subscription(db_sessionmaker, str(uid))
    assert sub is not None
    assert sub["status"] == "active"
    assert sub["plan"] == "vip"  # overwritten from 'pro'
    assert sub["expires_at"] > datetime.datetime.now(tz=datetime.UTC)


# ============================ Root-cause: policy unblock ============================
@pytest.mark.asyncio
async def test_root_cause_trial_used_unblocked_after_grant(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # ADR-002/ADR-048 root cause: subscription=none + trial_used + balance>0 is BLOCKED by
    # trial_used (credits are not even checked). Admin subscription grant must unblock it.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=True, subscription=None, balance=5)

    # BEFORE: /v1/policy/effective (user JWT) is blocked with trial_used.
    before = await admin_client.get("/v1/policy/effective", headers=auth_headers(uid))
    assert before.status_code == 200, before.text
    b = before.json()
    assert b["canGenerateCreditsMode"] is False
    assert "trial_used" in b["reasons"]

    # Admin activates the subscription (default credits).
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(uid), "expiresAt": _future_iso(30), "idempotencyKey": "rc"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text

    # AFTER: policy now allows credits mode — trial_used and credits_empty both gone.
    after = await admin_client.get("/v1/policy/effective", headers=auth_headers(uid))
    assert after.status_code == 200, after.text
    a = after.json()
    assert a["canGenerateCreditsMode"] is True
    assert a["isSubscribed"] is True
    assert "trial_used" not in a["reasons"]
    assert "credits_empty" not in a["reasons"]


# ============================ Idempotency & namespace ============================
@pytest.mark.asyncio
async def test_grant_idempotent_replay_no_double_credit(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None, balance=0)
    payload = {"userId": str(uid), "days": 30, "idempotencyKey": "s-dup", "credits": 100}
    r1 = await admin_client.post(
        "/v1/admin/subscription/grant", json=payload, headers=_ADMIN_HEADERS
    )
    r2 = await admin_client.post(
        "/v1/admin/subscription/grant", json=payload, headers=_ADMIN_HEADERS
    )
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert r1.json()["idempotentReplay"] is False
    assert r2.json()["idempotentReplay"] is True
    assert r1.json()["ledgerTxId"] == r2.json()["ledgerTxId"]  # same ledger tx
    assert await _balance(db_sessionmaker, str(uid)) == 100  # credited once

    credits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='credit'",
        str(uid),
    )
    assert credits == 1
    # Subscription upsert is idempotent by PK — still a single row.
    subs = await _count(
        db_sessionmaker, "SELECT count(*) FROM subscriptions WHERE user_id=:u", str(uid)
    )
    assert subs == 1


@pytest.mark.asyncio
async def test_ledger_key_namespace_isolated_from_wallet_grant(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Same raw idempotencyKey "shared" used on BOTH /v1/admin/wallet/grant (raw key) and
    # /v1/admin/subscription/grant (admin-sub-grant:{key}) must NOT collapse: two ledger rows.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None, balance=0)
    w = await admin_client.post(
        "/v1/admin/wallet/grant",
        json={"userId": str(uid), "amount": 50, "idempotencyKey": "shared", "reason": "x"},
        headers=_ADMIN_HEADERS,
    )
    assert w.status_code == 200, w.text
    s_grant = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(uid), "days": 30, "idempotencyKey": "shared", "credits": 30},
        headers=_ADMIN_HEADERS,
    )
    assert s_grant.status_code == 200, s_grant.text  # NOT a 409 despite same raw key
    assert s_grant.json()["idempotentReplay"] is False

    ledger = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='credit'",
        str(uid),
    )
    assert ledger == 2  # two distinct namespaced keys
    assert await _balance(db_sessionmaker, str(uid)) == 80  # 50 + 30
    # Verify the namespaced key is actually stored.
    ns = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions "
        "WHERE user_id=:u AND idempotency_key='admin-sub-grant:shared'",
        str(uid),
    )
    assert ns == 1


# ============================ 409 + full rollback (one transaction) ============================
@pytest.mark.asyncio
async def test_same_key_different_credits_409_full_rollback(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None, balance=0)
    # First grant: plan p1, days 30, credits 100.
    r1 = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "days": 30,
            "idempotencyKey": "s-conf",
            "plan": "p1",
            "credits": 100,
        },
        headers=_ADMIN_HEADERS,
    )
    assert r1.status_code == 200, r1.text
    sub_before = await _subscription(db_sessionmaker, str(uid))
    assert sub_before is not None and sub_before["plan"] == "p1"

    # Second grant: SAME key, DIFFERENT credits (200) and different plan/term -> 409 from
    # WalletService.grant; the whole request transaction rolls back (no partial apply).
    r2 = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={
            "userId": str(uid),
            "days": 90,
            "idempotencyKey": "s-conf",
            "plan": "p2",
            "credits": 200,
        },
        headers=_ADMIN_HEADERS,
    )
    assert r2.status_code == 409, r2.text

    # Subscription upsert of the 2nd call did NOT persist: plan/expires unchanged from the 1st.
    sub_after = await _subscription(db_sessionmaker, str(uid))
    assert sub_after is not None
    assert sub_after["plan"] == "p1"  # NOT 'p2'
    assert sub_after["expires_at"] == sub_before["expires_at"]  # unchanged term
    # Balance / ledger unchanged (single credit of 100).
    assert await _balance(db_sessionmaker, str(uid)) == 100
    credits = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='credit'",
        str(uid),
    )
    assert credits == 1


# ============================ 404 ============================
@pytest.mark.asyncio
async def test_unknown_user_404_no_side_effects(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    missing = uuid.uuid4()
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(missing), "days": 30, "idempotencyKey": "s-404"},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "user_not_found"
    # No phantom user / subscription / wallet.
    async with db_sessionmaker() as s:
        u = await s.scalar(text("SELECT count(*) FROM users WHERE id=:u"), {"u": str(missing)})
        sub = await s.scalar(
            text("SELECT count(*) FROM subscriptions WHERE user_id=:u"), {"u": str(missing)}
        )
        w = await s.scalar(
            text("SELECT count(*) FROM wallets WHERE user_id=:u"), {"u": str(missing)}
        )
    assert int(u) == 0 and int(sub) == 0 and int(w) == 0


# ============================ Validation (endpoint 422) ============================
_UID = str(uuid.uuid4())
_VALIDATION_BODIES = [
    {"days": 30, "idempotencyKey": "v"},  # missing userId
    {"userId": _UID, "expiresAt": _future_iso(), "days": 30, "idempotencyKey": "v"},  # both
    {"userId": _UID, "idempotencyKey": "v"},  # neither expiresAt nor days
    {"userId": _UID, "expiresAt": "2099-01-01T00:00:00", "idempotencyKey": "v"},  # naive tz
    {"userId": _UID, "expiresAt": "2000-01-01T00:00:00+00:00", "idempotencyKey": "v"},  # past
    {"userId": _UID, "days": 0, "idempotencyKey": "v"},  # days<=0
    {"userId": _UID, "days": 30, "idempotencyKey": "v", "credits": -1},  # credits<0
    {"userId": _UID, "days": 30, "idempotencyKey": "v", "extra": "x"},  # extra=forbid
    {"userId": _UID, "days": 30},  # missing idempotencyKey
    {"userId": _UID, "days": 30, "idempotencyKey": ""},  # empty idempotencyKey
]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", _VALIDATION_BODIES)
async def test_validation_422(admin_client: AsyncClient, payload: dict[str, Any]) -> None:
    r = await admin_client.post(
        "/v1/admin/subscription/grant", json=payload, headers=_ADMIN_HEADERS
    )
    assert r.status_code == 422, r.text


# ============================ Security ============================
@pytest.mark.asyncio
async def test_no_admin_token_401(admin_client: AsyncClient) -> None:
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(uuid.uuid4()), "days": 30, "idempotencyKey": "s"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_admin_token_401(admin_client: AsyncClient) -> None:
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(uuid.uuid4()), "days": 30, "idempotencyKey": "s"},
        headers={"X-Admin-Token": "nope"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_user_jwt_does_not_authorize(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None, balance=0)
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(uid), "days": 30, "idempotencyKey": "s"},
        headers=auth_headers(uid),  # valid user JWT, but no X-Admin-Token
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_body_over_8kb_413(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None, balance=0)
    padding = " " * (9 * 1024)  # inflate raw bytes past the 8 KB admin cap; JSON stays valid
    raw = f'{{"userId": "{uid}", "days": 30, "idempotencyKey": "big"{padding}}}'
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        content=raw.encode(),
        headers={**_ADMIN_HEADERS, "Content-Type": "application/json"},
    )
    assert r.status_code == 413, r.text


@pytest.mark.asyncio
async def test_admin_rate_limit_429(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None, balance=0)
    from app.api_gateway.routers import admin as admin_router

    async def _deny(**_kwargs: Any) -> bool:
        return False

    prev = admin_router.enforce_admin_limits
    admin_router.enforce_admin_limits = _deny  # type: ignore[assignment]
    try:
        r = await admin_client.post(
            "/v1/admin/subscription/grant",
            json={"userId": str(uid), "days": 30, "idempotencyKey": "rl"},
            headers=_ADMIN_HEADERS,
        )
        assert r.status_code == 429
    finally:
        admin_router.enforce_admin_limits = prev  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_audit_admin_subscription_grant_no_secret(
    admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None, balance=0)
    r = await admin_client.post(
        "/v1/admin/subscription/grant",
        json={"userId": str(uid), "days": 30, "idempotencyKey": "aud", "credits": 10},
        headers=_ADMIN_HEADERS,
    )
    assert r.status_code == 200, r.text

    # Both admin_subscription_grant (Admin) and billing_credit (Wallet) are written.
    admin_evt = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM audit_logs WHERE user_id=:u "
        "AND event_type='admin_subscription_grant'",
        str(uid),
    )
    billing = await _count(
        db_sessionmaker,
        "SELECT count(*) FROM audit_logs WHERE user_id=:u AND event_type='billing_credit'",
        str(uid),
    )
    assert admin_evt == 1
    assert billing == 1

    # admin_subscription_grant payload carries actor/plan/status/expiresAt/creditsGranted and a
    # ledgerTxId (credits>0); the admin secret is nowhere in any payload.
    async with db_sessionmaker() as s:
        payload = await s.scalar(
            text(
                "SELECT payload FROM audit_logs WHERE user_id=:u "
                "AND event_type='admin_subscription_grant'"
            ),
            {"u": str(uid)},
        )
        rows = await s.scalars(
            text("SELECT payload::text FROM audit_logs WHERE user_id=:u"), {"u": str(uid)}
        )
        blob = " ".join(rows)
    assert payload["actor"] == "admin"
    assert payload["status"] == "active"
    assert payload["creditsGranted"] == 10
    assert "expiresAt" in payload and "plan" in payload
    assert "ledgerTxId" in payload  # credits>0 -> present
    assert _ADMIN_SECRET not in blob
