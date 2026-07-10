"""Unit: Settings.resolved_deep_thinking_effort() / resolved_search_context_size() (ADR-055/059).

Pure config logic (no I/O). Settings is constructed directly with alias kwargs (same hermetic
pattern as test_model_selection_config_adr034.py) so each case is independent of the process env.
Both helpers normalize (strip().lower()) and degrade a malformed / empty value gracefully to the
safe default ``"medium"`` — a mis-configured env must NEVER crash startup nor forward an invalid
``reasoning.effort`` / ``search_context_size`` to OpenAI (ADR-055 §5 / ADR-059 §2,§3).
"""

from __future__ import annotations

import pytest

from app.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


# --------------------------- resolved_deep_thinking_effort() ---------------------------
@pytest.mark.parametrize("value", ["low", "medium", "high"])
def test_deep_thinking_effort_valid_values_pass_through(value: str) -> None:
    assert _settings(DEEP_THINKING_EFFORT=value).resolved_deep_thinking_effort() == value


def test_deep_thinking_effort_normalizes_case_and_whitespace() -> None:
    assert _settings(DEEP_THINKING_EFFORT="  HIGH  ").resolved_deep_thinking_effort() == "high"


@pytest.mark.parametrize("value", ["bogus", "extreme", "ultra", "0"])
def test_deep_thinking_effort_invalid_degrades_to_medium(value: str) -> None:
    assert _settings(DEEP_THINKING_EFFORT=value).resolved_deep_thinking_effort() == "medium"


def test_deep_thinking_effort_empty_degrades_to_medium() -> None:
    assert _settings(DEEP_THINKING_EFFORT="").resolved_deep_thinking_effort() == "medium"


def test_deep_thinking_effort_whitespace_only_degrades_to_medium() -> None:
    assert _settings(DEEP_THINKING_EFFORT="   ").resolved_deep_thinking_effort() == "medium"


def test_deep_thinking_effort_default_is_medium() -> None:
    # No env override → the field default ("medium") resolves to "medium".
    assert _settings().resolved_deep_thinking_effort() == "medium"


# --------------------------- resolved_search_context_size() ---------------------------
@pytest.mark.parametrize("value", ["low", "medium", "high"])
def test_search_context_size_valid_values_pass_through(value: str) -> None:
    assert _settings(OPENAI_SEARCH_CONTEXT_SIZE=value).resolved_search_context_size() == value


def test_search_context_size_normalizes_case_and_whitespace() -> None:
    assert _settings(OPENAI_SEARCH_CONTEXT_SIZE=" Low ").resolved_search_context_size() == "low"


@pytest.mark.parametrize("value", ["bogus", "huge", "medium-high"])
def test_search_context_size_invalid_degrades_to_medium(value: str) -> None:
    assert _settings(OPENAI_SEARCH_CONTEXT_SIZE=value).resolved_search_context_size() == "medium"


def test_search_context_size_empty_degrades_to_medium() -> None:
    assert _settings(OPENAI_SEARCH_CONTEXT_SIZE="").resolved_search_context_size() == "medium"


def test_search_context_size_whitespace_only_degrades_to_medium() -> None:
    assert _settings(OPENAI_SEARCH_CONTEXT_SIZE="   ").resolved_search_context_size() == "medium"


def test_search_context_size_default_is_medium() -> None:
    assert _settings().resolved_search_context_size() == "medium"


# --------------------------- no startup crash on malformed values ---------------------------
def test_malformed_dialog_mode_config_does_not_crash_construction() -> None:
    # A mis-configured env must construct cleanly (graceful degradation happens in the resolvers,
    # not at field validation) — no ValidationError / crash on Settings construction.
    s = _settings(DEEP_THINKING_EFFORT="nonsense", OPENAI_SEARCH_CONTEXT_SIZE="nonsense")
    assert s.resolved_deep_thinking_effort() == "medium"
    assert s.resolved_search_context_size() == "medium"
