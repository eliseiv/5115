"""Pure access-policy state machine (ADR-002).

`evaluate(state, mode)` is deterministic, side-effect-free and is the single source of
truth for both /chat/run and /policy/effective (AC-6, BR-6). Order of checks is fixed.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Mode(str, enum.Enum):
    credits = "credits"
    byok = "byok"


class SubscriptionStatus(str, enum.Enum):
    active = "active"
    expired = "expired"
    none = "none"


class ByokState(str, enum.Enum):
    missing = "missing"
    disabled = "disabled"
    invalid = "invalid"
    valid = "valid"
    # ADR-016 extended statuses. All non-valid, non-missing → treated as byok_invalid by policy.
    validating = "validating"
    offline = "offline"
    expired = "expired"


class BlockReason(str, enum.Enum):
    trial_used = "trial_used"
    subscription_required = "subscription_required"
    subscription_expired = "subscription_expired"
    credits_empty = "credits_empty"
    byok_disabled = "byok_disabled"
    byok_invalid = "byok_invalid"
    rate_limited = "rate_limited"
    policy_denied = "policy_denied"
    # ADR-025: response truncated by the output-token limit (stop_reason="max_tokens"). NOT a
    # policy reason and NOT a gateway concern — it is an orchestration outcome that fires AFTER
    # generation begins. `evaluate()` never returns it; it is set only by the orchestrator. Unlike
    # policy reasons it is excluded from /policy/effective.reasons[] (not predictable before
    # generation), and usage/messageStepId/stepId are present, credit is not debited.
    max_tokens = "max_tokens"
    # ADR-058 §5: insufficient credits for an image.generate debit (IMAGE_CREDITS_COST), credits
    # branch only. Like max_tokens it is an ORCHESTRATION outcome that fires AFTER generation
    # begins, NOT a policy reason — `evaluate()` never returns it, it is excluded from
    # /policy/effective.reasons[], and it is set only by the orchestrator (bytes not saved, the
    # image debit is rolled back). Distinct from credits_empty so the UI shows a profile-specific
    # CTA ("out of credits for image generation" vs "out of credits for a message").
    image_credits_empty = "image_credits_empty"


@dataclass(frozen=True)
class PolicyState:
    subscription_status: SubscriptionStatus
    trial_used: bool
    credits_balance: int
    byok_enabled: bool
    byok_status: ByokState


@dataclass(frozen=True)
class Decision:
    allow: bool
    block_reason: BlockReason | None = None

    @staticmethod
    def allowed() -> Decision:
        return Decision(allow=True, block_reason=None)

    @staticmethod
    def blocked(reason: BlockReason) -> Decision:
        return Decision(allow=False, block_reason=reason)


def _byok_state(byok_enabled: bool, byok_status: ByokState) -> ByokState:
    """Resolve effective byok state for the state machine.

    `disabled` takes precedence over key validity (BR-4 order: byok_disabled → byok_invalid).
    """
    if not byok_enabled:
        return ByokState.disabled
    return byok_status


def evaluate(state: PolicyState, mode: Mode) -> Decision:
    """Decide access per ADR-002. Pure: no I/O, no mutation."""
    if mode is Mode.byok:
        if state.subscription_status is not SubscriptionStatus.active:
            if state.subscription_status is SubscriptionStatus.expired:
                return Decision.blocked(BlockReason.subscription_expired)
            return Decision.blocked(BlockReason.subscription_required)
        effective = _byok_state(state.byok_enabled, state.byok_status)
        if effective is ByokState.disabled:
            return Decision.blocked(BlockReason.byok_disabled)
        # Only `valid` allows generation. Every other non-disabled state (missing/invalid plus
        # ADR-016 validating/offline/expired) yields byok_invalid (ADR-016 §Consequences).
        if effective is not ByokState.valid:
            return Decision.blocked(BlockReason.byok_invalid)
        return Decision.allowed()

    # mode == credits
    if state.subscription_status is SubscriptionStatus.active:
        if state.credits_balance == 0:
            return Decision.blocked(BlockReason.credits_empty)
        return Decision.allowed()  # debit happens after generation
    if state.subscription_status is SubscriptionStatus.expired:
        return Decision.blocked(BlockReason.subscription_expired)
    # subscription_status == none
    if state.trial_used:
        return Decision.blocked(BlockReason.trial_used)
    return Decision.allowed()  # the single lifetime trial
