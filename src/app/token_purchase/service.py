"""Token-purchase service: verify consumable transaction -> map productId -> idempotent grant.

Flow (token-purchase/03, ADR-015):
    require_active_subscription(userId)                    # Q-015-1=B: 403 subscription_required
    verify(transaction) -> { transactionId, productId }   # shared verifier (fail-closed)
    credits = TOKEN_PRODUCTS[productId]                    # unknown -> 422
    Wallet.grant(credits, idempotency_key="token-purchase:{transactionId}",
                 meta={source: "token_purchase", productId})

This is a thin wrapper over the StoreKit verifier (shared with subscription, unchanged) and
Wallet.grant (ADR-006). It never re-implements ledger logic: idempotency, balance update and
the billing_credit audit all come from WalletService.grant. The consumable path is distinct
from subscription/sync (auto-renewable) — separate endpoint, separate idempotency-key
namespace, and meta.source="token_purchase" disambiguates in the ledger/audit history.

Policy-guard (Q-015-1=B, ADR-015 §Доступность): the purchase only proceeds for a user with an
active subscription. The check is the *first* step — before verify and before Wallet.grant — so
no App Store Server API call is spent and no ledger row is written for an unsubscribed user.
Subscription status comes from the same source that feeds ``PolicyState.subscription_status``
(``load_policy_state``, ADR-002), including lazy expiry. The check is read-only and precedes the
single ledger write (Wallet.grant), so idempotency (``ux_ledger_idempotency``) is untouched.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import SubscriptionRequiredError, ValidationFailedError
from app.observability.metrics import token_purchase_total
from app.policy.engine import SubscriptionStatus
from app.policy.loader import load_policy_state
from app.subscription.storekit import StoreKitVerifier, VerifiedTransaction
from app.wallet.service import WalletService

# meta.source marker for credit-tx originating from a consumable token purchase (BR-TP-3).
TOKEN_PURCHASE_SOURCE = "token_purchase"
# Idempotency-key prefix keeps the consumable key namespace explicit and distinct from the
# subscription "sub-grant:" prefix (token-purchase/03; ADR-005).
_IDEMPOTENCY_PREFIX = "token-purchase:"
_GRANT_REASON = "token_purchase"


@dataclass(frozen=True)
class PurchaseResult:
    credits_added: int
    new_balance: int
    transaction_id: str


class TokenPurchaseService:
    """Processes a consumable StoreKit purchase into an idempotent credit grant."""

    def __init__(
        self, session: AsyncSession, verifier: StoreKitVerifier, wallet: WalletService
    ) -> None:
        self._session = session
        self._verifier = verifier
        self._wallet = wallet

    async def purchase(self, user_id: uuid.UUID, signed_transaction: str) -> PurchaseResult:
        """Verify the consumable transaction and grant the mapped credits (idempotently).

        Raises SubscriptionRequiredError (-> 403 subscription_required) when the user has no
        active subscription (Q-015-1=B): the policy-guard runs first, so neither verify nor
        Wallet.grant is invoked and no ledger row is written. Raises ValidationFailedError
        (-> 422) on an invalid/forged transaction or an unknown productId. On a repeated
        transaction the underlying grant is idempotent: credits_added is 0 and new_balance is
        the unchanged current balance (BR-TP-2).
        """
        await self._require_active_subscription(user_id)

        try:
            verified: VerifiedTransaction = self._verifier.verify(signed_transaction)
        except ValidationFailedError:
            token_purchase_total.labels(result="invalid_transaction").inc()
            raise

        credits = self._credits_for_product(verified.product_id)

        result = await self._wallet.grant(
            user_id=user_id,
            amount=credits,
            idempotency_key=f"{_IDEMPOTENCY_PREFIX}{verified.transaction_id}",
            meta={
                "source": TOKEN_PURCHASE_SOURCE,
                "productId": verified.product_id,
                "transactionId": verified.transaction_id,
            },
            reason=_GRANT_REASON,
        )

        # Idempotent replay: the transaction was already processed; nothing new was credited
        # (credits_added=0), balance is the current unchanged value (BR-TP-2, contract §32).
        credits_added = 0 if result.idempotent_replay else credits
        token_purchase_total.labels(
            result="replay" if result.idempotent_replay else "granted"
        ).inc()

        return PurchaseResult(
            credits_added=credits_added,
            new_balance=result.new_balance,
            transaction_id=verified.transaction_id,
        )

    async def _require_active_subscription(self, user_id: uuid.UUID) -> None:
        """Fail-fast policy-guard: purchase requires an active subscription (Q-015-1=B, ADR-015).

        Read-only check against the same source that feeds ``PolicyState.subscription_status``
        (``load_policy_state``, ADR-002), applying the same lazy-expiry rule. Runs before verify
        and before ``Wallet.grant`` — on failure no App Store API call is spent, no ledger row is
        written, and idempotency (``ux_ledger_idempotency``) is untouched. No active subscription
        -> 403 ``subscription_required`` (NOT a 200 blockReason — ADR-004 does not apply to
        top-up operations).
        """
        state = await load_policy_state(self._session, user_id)
        if state.subscription_status is not SubscriptionStatus.active:
            token_purchase_total.labels(result="subscription_required").inc()
            raise SubscriptionRequiredError("an active subscription is required to buy tokens")

    def _credits_for_product(self, product_id: str) -> int:
        """Resolve credits strictly from server-side TOKEN_PRODUCTS (BR-TP-1, BR-TP-5).

        Unknown productId -> 422 (never trust a credit count from the client body).
        """
        products = get_settings().token_products()
        credits = products.get(product_id)
        if credits is None:
            token_purchase_total.labels(result="unknown_product").inc()
            raise ValidationFailedError("unknown token product")
        return credits
