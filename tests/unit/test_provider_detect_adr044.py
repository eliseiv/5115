"""Unit tests for ``detect_byok_provider`` — multi-provider BYOK prefix detection (ADR-044 §1).

Pure function: no network, no logging, no raise. The detection order is load-bearing
(``sk-ant-`` BEFORE ``sk-``) and the result is restricted to the canonical provider set
{anthropic, openai} or ``None`` for an unrecognized format.
"""

from __future__ import annotations

import logging

import pytest

from app.byok.provider_detect import (
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    detect_byok_provider,
)


@pytest.mark.parametrize(
    ("api_key", "expected"),
    [
        # Anthropic prefix — MUST win over the generic sk- branch (priority 1 before 3).
        ("sk-ant-api03-abcdef", "anthropic"),
        ("sk-ant-", "anthropic"),
        ("sk-ant-anything-here", "anthropic"),
        # OpenAI project-scoped (priority 2) and generic sk- (priority 3) both → openai.
        ("sk-proj-abcdef123", "openai"),
        ("sk-proj-", "openai"),
        ("sk-abcdef123456", "openai"),
        ("sk-svcacct-xyz", "openai"),
        # Unrecognized formats → None (no provider; caller treats as terminal invalid, no network).
        ("", None),
        ("garbage", None),
        ("not-a-key", None),
        ("ant-sk-reversed", None),
        ("Sk-ant-uppercase-prefix", None),  # case-sensitive: only lowercase sk- prefixes match.
        ("xk-ant-wrong-first-letter", None),
        ("api-key-without-sk", None),
    ],
)
def test_detect_by_prefix(api_key: str, expected: str | None) -> None:
    assert detect_byok_provider(api_key) == expected


def test_anthropic_precedes_openai_order_is_load_bearing() -> None:
    """``sk-ant-...`` also starts with ``sk-``; the Anthropic check MUST run first (ADR-044 §1)."""
    # If the order were wrong this would misclassify as openai.
    assert detect_byok_provider("sk-ant-api03-realistic-anthropic-key") == PROVIDER_ANTHROPIC
    # A bare sk- (no ant) is openai — confirms the generic branch still fires.
    assert detect_byok_provider("sk-openai-style") == PROVIDER_OPENAI


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  sk-ant-padded  ", "anthropic"),
        ("\tsk-proj-tabbed\n", "openai"),
        ("  sk-generic  ", "openai"),
        ("   ", None),  # only-whitespace → empty after strip → None
        ("\n\t", None),
    ],
)
def test_strip_leading_trailing_whitespace(raw: str, expected: str | None) -> None:
    """Only ``strip()`` normalization is applied; the key body is never transformed (ADR-044 §1)."""
    assert detect_byok_provider(raw) == expected


def test_inner_whitespace_is_not_stripped() -> None:
    """Whitespace INSIDE the key is not removed → an embedded-space key is unrecognized."""
    assert detect_byok_provider("sk -ant-broken") is None


def test_pure_no_raise_on_any_input() -> None:
    """The function never raises for any string input (ADR-044 §1: pure, returns a value)."""
    for sample in ["", " ", "sk-", "sk-ant-", "💥", "sk-" * 1000]:
        # Must return without raising; value type is str|None.
        result = detect_byok_provider(sample)
        assert result in (PROVIDER_ANTHROPIC, PROVIDER_OPENAI, None)


def test_key_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Security (ADR-044 §8 / 05-security.md): the key value MUST NOT appear in any log record."""
    secret = "sk-ant-super-secret-value-should-not-be-logged-9911"
    with caplog.at_level(logging.DEBUG):
        detect_byok_provider(secret)
    assert secret not in caplog.text
    # No log records at all are emitted by this pure function.
    assert caplog.records == []


def test_return_values_are_the_canonical_constants() -> None:
    """The returned strings are exactly the module constants (single source of truth)."""
    assert detect_byok_provider("sk-ant-x") is PROVIDER_ANTHROPIC
    assert detect_byok_provider("sk-x") is PROVIDER_OPENAI
    assert PROVIDER_ANTHROPIC == "anthropic"
    assert PROVIDER_OPENAI == "openai"
