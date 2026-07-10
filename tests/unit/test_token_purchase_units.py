"""Unit tests for token-purchase (ADR-015, MVP).

Covers the pure, I/O-free pieces (token-purchase/09-testing.md §Unit):
- Settings.token_products() parsing/validation of the TOKEN_PRODUCTS mapping
  (BR-TP-1 anti-tamper: only positive-int credits under string keys survive; malformed
  JSON / non-object / bad entries are dropped → unknown productId 422 downstream).
- TokenPurchaseService._credits_for_product: productId → credits strictly from the server-side
  table; unknown productId → ValidationFailedError (422). The credit count is NEVER taken from
  the request body (the body has no credits field — substitution is impossible by construction).

These are pure: no DB, no HTTP. The service path is exercised against a stub verifier/wallet so
the mapping logic is isolated from the ledger.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.config import Settings, get_settings
from app.errors import ValidationFailedError
from app.schemas.token_purchase import TokenPurchaseRequest
from app.subscription.storekit import VerifiedTransaction
from app.token_purchase.service import TokenPurchaseService

# --- Settings.token_products(): the server-side productId -> credits source of truth -------


def _settings(raw: str) -> Settings:
    """A Settings with a controlled TOKEN_PRODUCTS payload (other fields default).

    The field uses the alias TOKEN_PRODUCTS; pydantic-settings maps constructor kwargs by alias,
    so pass it under the alias (not the python field name) to override env/.env for this test.
    """
    return Settings(TOKEN_PRODUCTS=raw)  # type: ignore[call-arg]


def test_token_products_valid_mapping_parsed() -> None:
    s = _settings('{"tokens_1500":1500,"tokens_600":600,"tokens_250":250,"tokens_100":100}')
    assert s.token_products() == {
        "tokens_1500": 1500,
        "tokens_600": 600,
        "tokens_250": 250,
        "tokens_100": 100,
    }


def test_token_products_empty_default_is_empty_mapping() -> None:
    # Empty default => no products configured => every purchase 422 until set.
    assert _settings("{}").token_products() == {}
    assert _settings("").token_products() == {}


def test_token_products_malformed_json_yields_empty_mapping() -> None:
    # Broken JSON must never produce a partial/ambiguous credit table — fail to empty.
    assert _settings("{not valid json").token_products() == {}


def test_token_products_non_object_json_yields_empty_mapping() -> None:
    # A JSON array / scalar is not a productId->credits object.
    assert _settings("[1,2,3]").token_products() == {}
    assert _settings('"tokens_100"').token_products() == {}
    assert _settings("1500").token_products() == {}


def test_token_products_drops_non_positive_credits() -> None:
    s = _settings('{"ok":100,"zero":0,"neg":-50}')
    assert s.token_products() == {"ok": 100}


def test_token_products_drops_bool_values() -> None:
    # bool is a subclass of int; True must NOT become 1 credit.
    s = _settings('{"ok":250,"flag_true":true,"flag_false":false}')
    assert s.token_products() == {"ok": 250}


def test_token_products_drops_non_int_credit_values() -> None:
    # Float / string credit values are not valid integer credit counts.
    s = _settings('{"ok":600,"floaty":12.5,"stringy":"100","nully":null}')
    assert s.token_products() == {"ok": 600}


def test_token_products_drops_non_string_keys() -> None:
    # JSON object keys are always strings, but a defensive check guards the table; a numeric-
    # looking key still arrives as a string and is kept, so this asserts the kept-string case.
    s = _settings('{"123":100}')
    assert s.token_products() == {"123": 100}


# --- TokenPurchaseService._credits_for_product: mapping + unknown -> 422 -------------------


class _StubVerifier:
    """Returns a scripted VerifiedTransaction; never touches Apple infra."""

    def __init__(self, product_id: str, transaction_id: str = "txn-unit-1") -> None:
        self._product_id = product_id
        self._transaction_id = transaction_id

    def verify(self, signed_transaction: str) -> VerifiedTransaction:
        return VerifiedTransaction(
            transaction_id=self._transaction_id,
            original_transaction_id=self._transaction_id,
            product_id=self._product_id,
            expires_at=None,
            revoked=False,
            environment="sandbox",
        )


def _service_with_products(
    monkeypatch: pytest.MonkeyPatch, product_id: str, raw_products: str
) -> TokenPurchaseService:
    """Build a service whose get_settings() reports a controlled TOKEN_PRODUCTS table.

    get_settings is lru_cached; clear it around the override and afterwards so the polluted
    Settings never leaks into the session-shared cache used by other tests.
    """
    monkeypatch.setenv("TOKEN_PRODUCTS", raw_products)
    get_settings.cache_clear()
    # session + wallet are irrelevant for the mapping branch (unknown product raises before the
    # policy-guard's DB read and before grant); pass sentinels.
    svc = TokenPurchaseService(
        None,  # type: ignore[arg-type]
        _StubVerifier(product_id),
        wallet=None,  # type: ignore[arg-type]
    )
    return svc


def test_credits_for_product_maps_known_product(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_PRODUCTS", '{"tokens_1500":1500}')
    get_settings.cache_clear()
    try:
        svc = TokenPurchaseService(None, _StubVerifier("tokens_1500"), wallet=None)  # type: ignore[arg-type]
        assert svc._credits_for_product("tokens_1500") == 1500
    finally:
        get_settings.cache_clear()


def test_credits_for_unknown_product_raises_422(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_PRODUCTS", '{"tokens_1500":1500}')
    get_settings.cache_clear()
    try:
        svc = TokenPurchaseService(None, _StubVerifier("tokens_unknown"), wallet=None)  # type: ignore[arg-type]
        with pytest.raises(ValidationFailedError, match="unknown token product"):
            svc._credits_for_product("tokens_unknown")
    finally:
        get_settings.cache_clear()


def test_credits_for_product_empty_table_raises_422(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_PRODUCTS", "{}")
    get_settings.cache_clear()
    try:
        svc = TokenPurchaseService(None, _StubVerifier("tokens_1500"), wallet=None)  # type: ignore[arg-type]
        with pytest.raises(ValidationFailedError):
            svc._credits_for_product("tokens_1500")
    finally:
        get_settings.cache_clear()


# --- Anti-tamper: the request body has no `credits` field (substitution impossible) --------


def test_request_schema_has_no_credits_field() -> None:
    # The contract body is {userId, transaction}; there is no credits field a client could set.
    fields = set(TokenPurchaseRequest.model_fields)
    assert fields == {"userId", "transaction"}
    assert "credits" not in fields


def test_request_schema_rejects_extra_credits_field() -> None:
    # StrictModel forbids unknown keys, so even a smuggled `credits` is rejected at the boundary.
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        TokenPurchaseRequest(
            userId=uuid.uuid4(),
            transaction="jws.token.here",
            credits=999999,  # type: ignore[call-arg]
        )


@pytest.mark.asyncio
async def test_purchase_uses_table_credits_not_any_body_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end of the credit-source rule at the service layer: the granted amount equals the
    server-side table value for the verified productId, independent of any transaction string."""
    monkeypatch.setenv("TOKEN_PRODUCTS", '{"tokens_600":600}')
    get_settings.cache_clear()

    # The policy-guard (Q-015-1=B) reads subscription status via load_policy_state before grant.
    # This unit test isolates the credit-source rule, so stub the guard to an active subscription
    # (the unsubscribed/expired -> 403 paths are covered by the integration suite).
    import app.token_purchase.service as tp_service
    from app.policy.engine import ByokState, PolicyState, SubscriptionStatus

    async def _active_state(_session: Any, _user_id: Any) -> PolicyState:
        return PolicyState(
            subscription_status=SubscriptionStatus.active,
            trial_used=True,
            credits_balance=0,
            byok_enabled=False,
            byok_status=ByokState.disabled,
        )

    monkeypatch.setattr(tp_service, "load_policy_state", _active_state)

    class _RecordingWallet:
        def __init__(self) -> None:
            self.granted_amount: int | None = None

        async def grant(self, **kwargs: Any) -> Any:
            from app.wallet.service import GrantResult

            self.granted_amount = kwargs["amount"]
            return GrantResult(new_balance=600, ledger_tx_id=uuid.uuid4(), idempotent_replay=False)

    try:
        wallet = _RecordingWallet()
        svc = TokenPurchaseService(None, _StubVerifier("tokens_600"), wallet=wallet)  # type: ignore[arg-type]
        # The "transaction" content is opaque to the credit decision — productId drives credits.
        result = await svc.purchase(uuid.uuid4(), "any-opaque-jws-string-with-no-credits")
        assert wallet.granted_amount == 600  # strictly from TOKEN_PRODUCTS, never the body
        assert result.credits_added == 600
    finally:
        get_settings.cache_clear()
