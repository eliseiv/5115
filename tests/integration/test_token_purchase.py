"""Integration: POST /v1/tokens/purchase + GET /v1/tokens/products (ADR-015, MVP).

Drives the FULL HTTP path through the REAL StoreKitVerifier in HS256 test-mode
(STOREKIT_TEST_MODE + STOREKIT_TEST_SECRET, TD-007 / 09-e2e-testing.md §2) against the real
PostgreSQL container — so verification, the productId→credits mapping, Wallet.grant idempotency
and the ledger row all execute end to end (token-purchase/09-testing.md §Integration).

The shared `client` fixture overrides the StoreKit singleton with the fake; here we deliberately
build our own app/client wired to a REAL verifier so the HS256 signature path is exercised. The
fake-based path is covered by the unit tests and the subscription suite.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from typing import Any

import jwt as pyjwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import auth_headers, seed_user

_TEST_SECRET = "token-purchase-storekit-secret"  # noqa: S105 - test-only HS256 secret
_BUNDLE_ID = "com.example.app"  # matches conftest APPSTORE_BUNDLE_ID
_PRODUCTS = '{"tokens_1500":1500,"tokens_600":600,"tokens_250":250,"tokens_100":100}'


def make_storekit_jws(
    *,
    transaction_id: str,
    product_id: str,
    secret: str = _TEST_SECRET,
    bundle_id: str = _BUNDLE_ID,
) -> str:
    """Mint a controlled HS256 'StoreKit' consumable transaction (test-mode branch)."""
    now = datetime.datetime.now(tz=datetime.UTC)
    payload: dict[str, Any] = {
        "transactionId": transaction_id,
        "originalTransactionId": transaction_id,
        "productId": product_id,
        "bundleId": bundle_id,
        "environment": "Sandbox",
        "type": "Consumable",
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(hours=1)).timestamp()),
    }
    return pyjwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
async def tp_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """ASGI client wired to a REAL StoreKitVerifier in HS256 test-mode + TOKEN_PRODUCTS set.

    Resets get_settings/ verifier singleton around the test so the test-mode posture never leaks
    into the session-shared cache used by other suites.
    """
    from app import deps
    from app.api_gateway import rate_limit
    from app.api_gateway.routers import subscription as sub_router
    from app.api_gateway.routers import wallet as wallet_router
    from app.main import create_app
    from app.subscription import storekit as storekit_mod

    monkeypatch.setenv("STOREKIT_TEST_MODE", "true")
    monkeypatch.setenv("STOREKIT_TEST_SECRET", _TEST_SECRET)
    monkeypatch.setenv("APPSTORE_BUNDLE_ID", _BUNDLE_ID)
    monkeypatch.setenv("APPSTORE_ROOT_CERT_DIR", "")
    monkeypatch.setenv("TOKEN_PRODUCTS", _PRODUCTS)
    get_settings.cache_clear()

    # Real verifier reading the patched env (HS256 branch active).
    real_verifier = storekit_mod.StoreKitVerifier()
    saved_singleton = storekit_mod._verifier_singleton
    storekit_mod._verifier_singleton = real_verifier

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
    wallet_router.enforce_other_limits = _allow  # type: ignore[assignment]
    sub_router.enforce_other_limits = _allow  # type: ignore[assignment]
    # token_purchase router imported enforce_other_limits at module load — patch there too.
    from app.api_gateway.routers import token_purchase as tp_router

    orig_tp = tp_router.enforce_other_limits
    tp_router.enforce_other_limits = _allow  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    rate_limit.enforce_other_limits = orig_other  # type: ignore[assignment]
    wallet_router.enforce_other_limits = orig_other  # type: ignore[assignment]
    sub_router.enforce_other_limits = orig_other  # type: ignore[assignment]
    tp_router.enforce_other_limits = orig_tp  # type: ignore[assignment]
    storekit_mod._verifier_singleton = saved_singleton
    get_settings.cache_clear()


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int | None:
    async with maker() as s:
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
        return None if bal is None else int(bal)


# --- Happy path: valid consumable -> grant -------------------------------------------------


@pytest.mark.asyncio
async def test_valid_purchase_grants_mapped_credits(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        # Active subscription required to buy tokens (Q-015-1=B policy-guard, ADR-015).
        uid = await seed_user(s, subscription="active", balance=730)  # pre-existing balance
    jws = make_storekit_jws(transaction_id="tx-tp-1", product_id="tokens_1500")
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["creditsAdded"] == 1500
    assert body["newBalance"] == 2230  # 730 + 1500
    assert body["transactionId"] == "tx-tp-1"
    assert await _balance(db_sessionmaker, uid) == 2230


@pytest.mark.asyncio
async def test_purchase_creates_ledger_credit_with_token_purchase_source(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active")
    jws = make_storekit_jws(transaction_id="tx-tp-meta", product_id="tokens_600")
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
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
    assert int(row.amount) == 600
    assert row.meta["source"] == "token_purchase"
    assert row.meta["productId"] == "tokens_600"
    # consumable idempotency-key namespace is distinct from subscription's "sub-grant:".
    assert row.idempotency_key == "token-purchase:tx-tp-meta"


# --- Idempotency: replay the same transactionId -> creditsAdded=0, balance unchanged --------


@pytest.mark.asyncio
async def test_replay_same_transaction_is_idempotent(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active")
    jws = make_storekit_jws(transaction_id="tx-tp-dup", product_id="tokens_250")

    r1 = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert r1.status_code == 200
    assert r1.json()["creditsAdded"] == 250
    assert r1.json()["newBalance"] == 250

    # Re-submit the identical transaction: nothing new credited, balance is the current value.
    r2 = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert r2.status_code == 200
    assert r2.json()["creditsAdded"] == 0  # idempotent replay
    assert r2.json()["newBalance"] == 250  # unchanged

    # Exactly one credit ledger row exists for this transaction.
    async with db_sessionmaker() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='credit'"),
            {"u": str(uid)},
        )
    assert int(n) == 1
    assert await _balance(db_sessionmaker, uid) == 250


# --- Disambiguation: token-purchase grant vs subscription grant do not conflict -------------


@pytest.mark.asyncio
async def test_token_purchase_and_subscription_grants_coexist(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A subscription period grant and a token purchase with the SAME underlying transactionId
    string must not collide: they live in distinct idempotency-key namespaces
    ("sub-grant:" vs "token-purchase:") and the subscription grant's behaviour is unchanged."""
    from app.audit.service import AuditService
    from app.wallet.service import WalletService

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active")
        # Simulate a prior subscription period grant (sub-grant: namespace, meta without source).
        wallet = WalletService(s, AuditService(s))
        sub_res = await wallet.grant(
            user_id=uid,
            amount=1000,
            idempotency_key="sub-grant:shared-tx-id",
            meta={
                "reason": "subscription_period",
                "transactionId": "shared-tx-id",
                "productId": "pro_monthly",
            },
            reason="subscription_period",
        )
        await s.commit()
    assert sub_res.new_balance == 1000

    # Token purchase using the SAME transactionId string — distinct key, must still grant.
    jws = make_storekit_jws(transaction_id="shared-tx-id", product_id="tokens_100")
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert r.json()["creditsAdded"] == 100
    assert r.json()["newBalance"] == 1100  # 1000 (subscription) + 100 (token purchase)

    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT idempotency_key, amount, meta FROM ledger_transactions "
                    "WHERE user_id=:u ORDER BY idempotency_key"
                ),
                {"u": str(uid)},
            )
        ).all()
    keys = {row.idempotency_key for row in rows}
    assert keys == {"sub-grant:shared-tx-id", "token-purchase:shared-tx-id"}
    by_key = {row.idempotency_key: row for row in rows}
    # subscription grant unchanged (no token_purchase source leaked onto it).
    assert by_key["sub-grant:shared-tx-id"].meta.get("source") is None
    assert int(by_key["sub-grant:shared-tx-id"].amount) == 1000
    # token-purchase grant carries the disambiguating source marker.
    assert by_key["token-purchase:shared-tx-id"].meta["source"] == "token_purchase"
    assert int(by_key["token-purchase:shared-tx-id"].amount) == 100


# --- Sad paths: forged transaction, unknown product, owner mismatch -------------------------


@pytest.mark.asyncio
async def test_forged_transaction_returns_422(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        # WITH an active subscription the guard passes, so verify runs and rejects the forged
        # transaction with 422 (Q-015-1=B: guard before verify; unsubscribed -> 403 instead).
        uid = await seed_user(s, subscription="active")
    # HS256 signed with the WRONG secret → invalid signature → 422 (same as a forged real one).
    forged = make_storekit_jws(
        transaction_id="tx-forged", product_id="tokens_1500", secret="wrong-secret"
    )
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": forged},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert await _balance(db_sessionmaker, uid) is None  # nothing granted


@pytest.mark.asyncio
async def test_unknown_product_returns_422(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active")
    jws = make_storekit_jws(transaction_id="tx-unknown", product_id="tokens_does_not_exist")
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert await _balance(db_sessionmaker, uid) is None


@pytest.mark.asyncio
async def test_user_id_mismatch_returns_403(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s)
        other = await seed_user(s)
    jws = make_storekit_jws(transaction_id="tx-403", product_id="tokens_1500")
    # Authenticated as `other`, but body claims the purchase is for `owner` → 403.
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(owner), "transaction": jws},
        headers=auth_headers(other),
    )
    assert r.status_code == 403, r.text
    assert await _balance(db_sessionmaker, owner) is None


@pytest.mark.asyncio
async def test_purchase_requires_auth(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    jws = make_storekit_jws(transaction_id="tx-noauth", product_id="tokens_1500")
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
    )
    assert r.status_code == 401, r.text


# --- Policy-guard (Q-015-1 = вариант B): purchase requires an active subscription ----------
# Spec: token-purchase/09-testing.md §Integration; ADR-015 §Доступность. The guard is the
# FIRST step (before verify, before Wallet.grant): an unsubscribed/expired user gets
# 403 {code: subscription_required}, no App Store call is spent, NO ledger row is written and
# the balance is untouched. Idempotency is preserved for subscribers.


async def _ledger_count(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u"), {"u": str(uid)}
        )
    return int(n)


@pytest.mark.asyncio
async def test_active_subscriber_purchase_grants_credits(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Active subscription -> guard passes -> credits granted, balance grows (Q-015-1=B)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=100)
    jws = make_storekit_jws(transaction_id="tx-sub-ok", product_id="tokens_600")
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert r.json()["creditsAdded"] == 600  # creditsAdded == package credits
    assert r.json()["newBalance"] == 700  # 100 + 600, balance grew
    assert await _balance(db_sessionmaker, uid) == 700
    assert await _ledger_count(db_sessionmaker, uid) == 1


@pytest.mark.asyncio
async def test_no_subscription_returns_403_subscription_required(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """No subscription row (status=none) -> 403 subscription_required; verifier NOT called,
    ledger empty, balance untouched."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None, balance=50)
    jws = make_storekit_jws(transaction_id="tx-nosub", product_id="tokens_1500")
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "subscription_required"
    # Guard ran before verify/grant: no ledger row written, balance unchanged.
    assert await _ledger_count(db_sessionmaker, uid) == 0
    assert await _balance(db_sessionmaker, uid) == 50


@pytest.mark.asyncio
async def test_status_none_no_subscription_row_returns_403(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Explicit subscriptions.status='none' row -> still 403 subscription_required."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="none")
    jws = make_storekit_jws(transaction_id="tx-statusnone", product_id="tokens_250")
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "subscription_required"
    assert await _ledger_count(db_sessionmaker, uid) == 0


@pytest.mark.asyncio
async def test_expired_subscription_lazy_expiry_returns_403(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """status='active' but expires_at in the past -> lazy expiry -> expired -> 403.

    load_policy_state applies the same lazy-expiry rule that feeds PolicyState, so an
    'active' row whose expires_at already passed is treated as expired and the guard denies."""
    async with db_sessionmaker() as s:
        # active row but already expired (expires_in_hours negative -> expires_at <= now).
        uid = await seed_user(s, subscription="active", expires_in_hours=-1)
    jws = make_storekit_jws(transaction_id="tx-expired", product_id="tokens_100")
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "subscription_required"
    assert await _ledger_count(db_sessionmaker, uid) == 0


@pytest.mark.asyncio
async def test_guard_runs_before_verify_forged_without_subscription_is_403_not_422(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Guard precedes verify: a FORGED transaction from an UNSUBSCRIBED user -> 403
    subscription_required (NOT 422). The verifier is never reached."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None)
    forged = make_storekit_jws(
        transaction_id="tx-forged-nosub", product_id="tokens_1500", secret="wrong-secret"
    )
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": forged},
        headers=auth_headers(uid),
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "subscription_required"
    assert await _ledger_count(db_sessionmaker, uid) == 0


@pytest.mark.asyncio
async def test_guard_before_verify_forged_with_subscription_is_422(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The complement: WITH an active subscription the guard passes and the forged transaction
    is rejected by verify -> 422 (not 403)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active")
    forged = make_storekit_jws(
        transaction_id="tx-forged-sub", product_id="tokens_1500", secret="wrong-secret"
    )
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": forged},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert await _ledger_count(db_sessionmaker, uid) == 0


@pytest.mark.asyncio
async def test_subscriber_replay_is_idempotent(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Idempotency is preserved through the guard for a subscriber: replaying the same
    transactionId -> creditsAdded=0, balance does not grow, no second ledger row."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active")
    jws = make_storekit_jws(transaction_id="tx-sub-dup", product_id="tokens_250")

    r1 = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["creditsAdded"] == 250
    assert r1.json()["newBalance"] == 250

    r2 = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["creditsAdded"] == 0  # idempotent replay, nothing new credited
    assert r2.json()["newBalance"] == 250  # balance did not grow
    assert await _ledger_count(db_sessionmaker, uid) == 1  # no second row


@pytest.mark.asyncio
async def test_subscription_required_metric_increments_on_denied_purchase(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """token_purchase_total{result="subscription_required"} increments when the guard denies."""
    from app.observability.metrics import token_purchase_total

    before = token_purchase_total.labels(result="subscription_required")._value.get()
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None)
    jws = make_storekit_jws(transaction_id="tx-metric", product_id="tokens_600")
    r = await tp_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert r.status_code == 403, r.text
    after = token_purchase_total.labels(result="subscription_required")._value.get()
    assert after == before + 1


# --- GET /v1/tokens/products : catalog -----------------------------------------------------


@pytest.mark.asyncio
async def test_products_catalog_returned(
    tp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await tp_client.get("/v1/tokens/products", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    products = {p["productId"]: p["credits"] for p in r.json()["products"]}
    assert products == {
        "tokens_1500": 1500,
        "tokens_600": 600,
        "tokens_250": 250,
        "tokens_100": 100,
    }


@pytest.mark.asyncio
async def test_products_catalog_requires_auth(tp_client: AsyncClient) -> None:
    r = await tp_client.get("/v1/tokens/products")
    assert r.status_code == 401, r.text
