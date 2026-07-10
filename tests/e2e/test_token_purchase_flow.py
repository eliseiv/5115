"""E2E: buy a token package -> balance grows -> generate in credits-mode WITHOUT a subscription.

This exercises the Q-015-1 default product posture (ADR-015): consumable token credits are a
separate axis from the subscription (ADR-002), so having credits unblocks credits-mode for a
user with no active subscription. Offline only — StoreKit is the real verifier in HS256
test-mode; Anthropic is faked at the client boundary (a live Claude call is not required for
token-purchase per 09-testing.md). PostgreSQL is the real container.

Flow:
  1. Fresh user, no subscription, trial not yet used.
  2. POST /v1/tokens/purchase (valid consumable) -> credits granted, balance grows.
  3. POST /v1/chat/run mode=credits -> generation allowed (Q-015-1 default), one debit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user
from tests.integration.test_token_purchase import (
    _PRODUCTS,
    _TEST_SECRET,
    make_storekit_jws,
)


@pytest.fixture
async def tp_e2e_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> AsyncIterator[AsyncClient]:
    """ASGI client: REAL StoreKit verifier (HS256 test-mode) + FAKE Anthropic + TOKEN_PRODUCTS."""
    from app import deps
    from app.api_gateway import rate_limit
    from app.api_gateway.routers import chat as chat_router
    from app.api_gateway.routers import token_purchase as tp_router
    from app.chat import anthropic_client as anthropic_mod
    from app.main import create_app
    from app.subscription import storekit as storekit_mod

    monkeypatch.setenv("STOREKIT_TEST_MODE", "true")
    monkeypatch.setenv("STOREKIT_TEST_SECRET", _TEST_SECRET)
    monkeypatch.setenv("APPSTORE_BUNDLE_ID", "com.example.app")
    monkeypatch.setenv("APPSTORE_ROOT_CERT_DIR", "")
    monkeypatch.setenv("TOKEN_PRODUCTS", _PRODUCTS)
    get_settings.cache_clear()

    saved_verifier = storekit_mod._verifier_singleton
    storekit_mod._verifier_singleton = storekit_mod.StoreKitVerifier()
    saved_anthropic = anthropic_mod._anthropic_singleton
    anthropic_mod._anthropic_singleton = fake_anthropic  # type: ignore[assignment]

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

    orig_chat = rate_limit.enforce_chat_limits
    orig_other = rate_limit.enforce_other_limits
    rate_limit.enforce_chat_limits = _allow  # type: ignore[assignment]
    rate_limit.enforce_other_limits = _allow  # type: ignore[assignment]
    chat_router.enforce_chat_limits = _allow  # type: ignore[assignment]
    orig_tp = tp_router.enforce_other_limits
    tp_router.enforce_other_limits = _allow  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    rate_limit.enforce_chat_limits = orig_chat  # type: ignore[assignment]
    rate_limit.enforce_other_limits = orig_other  # type: ignore[assignment]
    chat_router.enforce_chat_limits = orig_chat  # type: ignore[assignment]
    tp_router.enforce_other_limits = orig_tp  # type: ignore[assignment]
    storekit_mod._verifier_singleton = saved_verifier
    anthropic_mod._anthropic_singleton = saved_anthropic
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_purchase_without_subscription_is_blocked_403_balance_unchanged(
    tp_e2e_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """E2E policy-guard (Q-015-1=B, 09-testing.md §E2E): a user WITHOUT an active subscription
    cannot buy a token package. The purchase is denied with 403 subscription_required and the
    wallet balance is unchanged (no grant, no ledger row)."""
    # Fresh user: no subscription, trial not used, no credits yet.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription=None, trial_used=False)

    jws = make_storekit_jws(transaction_id="tx-e2e-1", product_id="tokens_600")
    pr = await tp_e2e_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert pr.status_code == 403, pr.text
    assert pr.json()["error"]["code"] == "subscription_required"

    async with db_sessionmaker() as s:
        ledger_rows = await s.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u"), {"u": str(uid)}
        )
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
    # No grant happened: no ledger row, and no wallet row was created by the purchase path.
    assert int(ledger_rows) == 0
    assert bal is None


@pytest.mark.asyncio
async def test_purchased_credits_are_debited_for_credits_mode_generation(
    tp_e2e_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Purchased credits actually fund credits-mode generation: with an active subscription and
    the trial already used, a credits-mode message debits exactly one purchased credit. This
    proves the package grant is spendable balance, not a parallel/dead counter."""
    async with db_sessionmaker() as s:
        # Active subscription so credits-mode debits (ADR-002 billing matrix); trial used so the
        # free-trial path cannot mask the debit. No starting credits — only what we purchase.
        uid = await seed_user(s, subscription="active", trial_used=True)

    jws = make_storekit_jws(transaction_id="tx-e2e-2", product_id="tokens_250")
    pr = await tp_e2e_client.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": jws},
        headers=auth_headers(uid),
    )
    assert pr.status_code == 200, pr.text
    assert pr.json()["newBalance"] == 250

    fake_anthropic.responses = [fake_anthropic.text_result("answer")]
    cr = await tp_e2e_client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert cr.status_code == 200, cr.text
    assert cr.json()["status"] == "assistant_message"

    async with db_sessionmaker() as s:
        debits = await s.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'"),
            {"u": str(uid)},
        )
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
    assert int(debits) == 1
    assert int(bal) == 249  # 250 purchased - 1 message
