"""PolicyState loader and /policy/effective service (policy-engine/03,04)."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BYOKKey, Subscription, User, Wallet
from app.policy.engine import (
    BlockReason,
    ByokState,
    Decision,
    Mode,
    PolicyState,
    SubscriptionStatus,
    evaluate,
)


@dataclass(frozen=True)
class EffectivePolicy:
    is_subscribed: bool
    trial_remaining: int
    credits_balance: int
    byok_enabled: bool
    can_generate_credits_mode: bool
    can_generate_byok_mode: bool
    reasons: list[BlockReason]


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def _effective_subscription_status(
    status: str | None, expires_at: datetime.datetime | None
) -> SubscriptionStatus:
    """Lazy expiry: active with expires_at <= now() is treated as expired (policy-engine/04)."""
    if status is None or status == "none":
        return SubscriptionStatus.none
    if status == "expired":
        return SubscriptionStatus.expired
    # stored active
    if expires_at is not None and expires_at <= _now():
        return SubscriptionStatus.expired
    return SubscriptionStatus.active


async def load_policy_state(session: AsyncSession, user_id: uuid.UUID) -> PolicyState:
    """Single batched read of subscription/wallet/byok/user → PolicyState."""
    user = await session.get(User, user_id)
    trial_used = bool(user.trial_used) if user is not None else False

    sub = await session.scalar(select(Subscription).where(Subscription.user_id == user_id))
    sub_status = _effective_subscription_status(
        sub.status if sub else None, sub.expires_at if sub else None
    )

    wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    balance = int(wallet.balance) if wallet is not None else 0

    byok = await session.scalar(select(BYOKKey).where(BYOKKey.user_id == user_id))
    if byok is None:
        byok_enabled = False
        byok_status = ByokState.missing
    else:
        byok_enabled = bool(byok.enabled)
        byok_status = ByokState(byok.key_status)

    return PolicyState(
        subscription_status=sub_status,
        trial_used=trial_used,
        credits_balance=balance,
        byok_enabled=byok_enabled,
        byok_status=byok_status,
    )


async def effective(session: AsyncSession, user_id: uuid.UUID) -> EffectivePolicy:
    """Compute /policy/effective using the same evaluate() as /chat/run (AC-6)."""
    state = await load_policy_state(session, user_id)
    credits_decision: Decision = evaluate(state, Mode.credits)
    byok_decision: Decision = evaluate(state, Mode.byok)

    reasons: list[BlockReason] = []
    if not credits_decision.allow and credits_decision.block_reason is not None:
        reasons.append(credits_decision.block_reason)
    if (
        not byok_decision.allow
        and byok_decision.block_reason is not None
        and byok_decision.block_reason not in reasons
    ):
        reasons.append(byok_decision.block_reason)

    is_subscribed = state.subscription_status is SubscriptionStatus.active
    trial_remaining = (
        1 if (state.subscription_status is SubscriptionStatus.none and not state.trial_used) else 0
    )
    byok_enabled_effective = state.byok_enabled and state.byok_status is ByokState.valid

    return EffectivePolicy(
        is_subscribed=is_subscribed,
        trial_remaining=trial_remaining,
        credits_balance=state.credits_balance,
        byok_enabled=byok_enabled_effective,
        can_generate_credits_mode=credits_decision.allow,
        can_generate_byok_mode=byok_decision.allow,
        reasons=reasons,
    )
