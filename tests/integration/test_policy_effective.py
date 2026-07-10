"""Integration: /policy/effective consistent with the real /chat/run decision (AC-6/AC-8).

For every seeded state, canGenerate{Credits,Byok}Mode from the loader must equal the
Policy Engine decision that /chat/run would use (same evaluate()).
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.policy.engine import Mode, evaluate
from app.policy.loader import effective, load_policy_state
from tests.conftest import seed_user

# (subscription, trial_used, balance, byok_enabled, byok_status)
_SCENARIOS = [
    (None, False, None, False, None),  # fresh trial user
    (None, True, None, False, None),  # trial used
    ("active", True, 0, False, None),  # active, no credits
    ("active", True, 5, False, None),  # active, credits
    ("active", True, 0, True, "valid"),  # active, byok valid
    ("active", True, 0, True, "invalid"),  # active, byok invalid
    ("active", True, 0, False, "valid"),  # active, byok stored but disabled
    ("expired", True, 5, True, "valid"),  # expired
]


@pytest.mark.asyncio
@pytest.mark.parametrize("sub,trial,bal,byok_en,byok_st", _SCENARIOS)
async def test_effective_matches_evaluate(
    db_session: AsyncSession,
    sub: str | None,
    trial: bool,
    bal: int | None,
    byok_en: bool,
    byok_st: str | None,
) -> None:
    uid = await seed_user(
        db_session,
        trial_used=trial,
        subscription=sub,
        balance=bal,
        byok_enabled=byok_en,
        byok_status=byok_st,
    )

    state = await load_policy_state(db_session, uid)
    eff = await effective(db_session, uid)

    # The loader's can-generate flags must be exactly what evaluate() returns.
    assert eff.can_generate_credits_mode == evaluate(state, Mode.credits).allow
    assert eff.can_generate_byok_mode == evaluate(state, Mode.byok).allow


@pytest.mark.asyncio
async def test_effective_trial_remaining_and_subscribed(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, trial_used=False)
    eff = await effective(db_session, uid)
    assert eff.trial_remaining == 1
    assert eff.is_subscribed is False
    assert eff.can_generate_credits_mode is True

    uid2 = await seed_user(db_session, subscription="active", balance=5)
    eff2 = await effective(db_session, uid2)
    assert eff2.is_subscribed is True
    assert eff2.trial_remaining == 0


@pytest.mark.asyncio
async def test_lazy_expiry_treats_past_active_as_expired(db_session: AsyncSession) -> None:
    # stored active but expires_at in the past → effective expired (policy-engine/04).
    uid = await seed_user(db_session, subscription="active", expires_in_hours=-1, balance=5)
    state = await load_policy_state(db_session, uid)
    assert state.subscription_status.value == "expired"
    eff = await effective(db_session, uid)
    assert eff.can_generate_credits_mode is False
