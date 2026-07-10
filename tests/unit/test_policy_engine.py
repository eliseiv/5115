"""Unit tests for the pure Policy Engine state machine (ADR-002, AC-1..AC-3, AC-6).

Full cartesian product of {subscription} x {trial_used} x {credits} x {byok} x {mode}
per 06-testing-strategy.md.
"""

from __future__ import annotations

import itertools

import pytest

from app.policy.engine import (
    BlockReason,
    ByokState,
    Mode,
    PolicyState,
    SubscriptionStatus,
    evaluate,
)


def _expected(
    sub: SubscriptionStatus,
    trial_used: bool,
    credits: int,
    byok_enabled: bool,
    byok_status: ByokState,
    mode: Mode,
) -> tuple[bool, BlockReason | None]:
    """Reference oracle for ADR-002, independent of the implementation order."""
    if mode is Mode.byok:
        if sub is SubscriptionStatus.expired:
            return False, BlockReason.subscription_expired
        if sub is SubscriptionStatus.none:
            return False, BlockReason.subscription_required
        # active
        if not byok_enabled:
            return False, BlockReason.byok_disabled
        if byok_status is ByokState.disabled:
            return False, BlockReason.byok_disabled
        # ADR-016: only `valid` allows generation. Every other non-disabled status
        # (missing/invalid + extended validating/offline/expired) → byok_invalid.
        if byok_status is not ByokState.valid:
            return False, BlockReason.byok_invalid
        return True, None
    # credits
    if sub is SubscriptionStatus.active:
        if credits == 0:
            return False, BlockReason.credits_empty
        return True, None
    if sub is SubscriptionStatus.expired:
        return False, BlockReason.subscription_expired
    # none
    if trial_used:
        return False, BlockReason.trial_used
    return True, None


_ALL_STATES = list(
    itertools.product(
        list(SubscriptionStatus),
        [True, False],
        [0, 5],
        [True, False],
        list(ByokState),
        list(Mode),
    )
)


@pytest.mark.parametrize("sub,trial_used,credits,byok_enabled,byok_status,mode", _ALL_STATES)
def test_state_machine_full_matrix(
    sub: SubscriptionStatus,
    trial_used: bool,
    credits: int,
    byok_enabled: bool,
    byok_status: ByokState,
    mode: Mode,
) -> None:
    state = PolicyState(
        subscription_status=sub,
        trial_used=trial_used,
        credits_balance=credits,
        byok_enabled=byok_enabled,
        byok_status=byok_status,
    )
    decision = evaluate(state, mode)
    exp_allow, exp_reason = _expected(sub, trial_used, credits, byok_enabled, byok_status, mode)
    assert decision.allow is exp_allow
    assert decision.block_reason == exp_reason


# --- AC-1: trial lifetime once, credits mode ---
def test_trial_available_once_then_blocked() -> None:
    fresh = PolicyState(SubscriptionStatus.none, False, 0, False, ByokState.missing)
    assert evaluate(fresh, Mode.credits).allow is True

    used = PolicyState(SubscriptionStatus.none, True, 0, False, ByokState.missing)
    d = evaluate(used, Mode.credits)
    assert d.allow is False
    assert d.block_reason is BlockReason.trial_used


def test_trial_allow_does_not_depend_on_credits() -> None:
    # balance=0 must NOT yield credits_empty for an unsubscribed first-time user (AC-1).
    fresh = PolicyState(SubscriptionStatus.none, False, 0, False, ByokState.missing)
    d = evaluate(fresh, Mode.credits)
    assert d.allow is True
    assert d.block_reason is None


# --- AC-2: expired subscription blocks both modes ---
@pytest.mark.parametrize("mode", list(Mode))
def test_expired_subscription_blocks_every_mode(mode: Mode) -> None:
    state = PolicyState(SubscriptionStatus.expired, False, 100, True, ByokState.valid)
    d = evaluate(state, mode)
    assert d.allow is False
    assert d.block_reason is BlockReason.subscription_expired


def test_no_subscription_byok_requires_subscription() -> None:
    state = PolicyState(SubscriptionStatus.none, True, 0, True, ByokState.valid)
    d = evaluate(state, Mode.byok)
    assert d.allow is False
    assert d.block_reason is BlockReason.subscription_required


# --- AC-3: active subscription, credits gate ---
def test_active_credits_zero_blocks() -> None:
    state = PolicyState(SubscriptionStatus.active, True, 0, False, ByokState.missing)
    d = evaluate(state, Mode.credits)
    assert d.allow is False
    assert d.block_reason is BlockReason.credits_empty


def test_active_credits_positive_allows() -> None:
    state = PolicyState(SubscriptionStatus.active, True, 1, False, ByokState.missing)
    assert evaluate(state, Mode.credits).allow is True


def test_active_byok_valid_allows_even_with_zero_credits() -> None:
    state = PolicyState(SubscriptionStatus.active, True, 0, True, ByokState.valid)
    assert evaluate(state, Mode.byok).allow is True


def test_byok_disabled_precedes_invalid() -> None:
    # enabled=False with status=invalid → byok_disabled wins (BR-4 order).
    state = PolicyState(SubscriptionStatus.active, True, 0, False, ByokState.invalid)
    d = evaluate(state, Mode.byok)
    assert d.block_reason is BlockReason.byok_disabled
