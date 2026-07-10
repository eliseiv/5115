"""Unit: Settings.allowed_models() / default_model() — model allowlist (ADR-034 §1).

Pure config logic (no I/O). Settings is constructed directly with alias kwargs (same pattern as
test_attachments.py / test_billing_adapty_parser.py) so each case is hermetic and independent of
the process env. Covers:
- empty allowlist → {default_model(): default_model()} (backward-compatible single default entry);
- non-empty allowlist WITHOUT the default → default prepended FIRST (insertion order preserved);
- non-empty allowlist WITH the default → returned as-is (order preserved, no duplicate);
- shape rules (token_products parity): non-str values / blank keys / non-object / bad JSON dropped;
- provider selection: anthropic vs openai raw chosen by LLM_PROVIDER, default_model() per provider.
"""

from __future__ import annotations

import json

from app.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


# --------------------------- default_model() per provider ---------------------------
def test_default_model_anthropic_is_anthropic_model() -> None:
    s = _settings(LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-sonnet-4-5")
    assert s.default_model() == "claude-sonnet-4-5"


def test_default_model_openai_is_openai_model() -> None:
    s = _settings(LLM_PROVIDER="openai", OPENAI_MODEL="gpt-4o")
    assert s.default_model() == "gpt-4o"


def test_default_model_provider_case_insensitive() -> None:
    s = _settings(LLM_PROVIDER="OpenAI", OPENAI_MODEL="gpt-4o", ANTHROPIC_MODEL="claude-x")
    assert s.default_model() == "gpt-4o"


# --------------------------- empty allowlist → single default entry ---------------------------
def test_empty_allowlist_falls_back_to_single_default_anthropic() -> None:
    s = _settings(LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-sonnet-4-5")
    assert s.allowed_models() == {"claude-sonnet-4-5": "claude-sonnet-4-5"}


def test_empty_allowlist_falls_back_to_single_default_openai() -> None:
    s = _settings(LLM_PROVIDER="openai", OPENAI_MODEL="gpt-4o", OPENAI_MODELS="{}")
    assert s.allowed_models() == {"gpt-4o": "gpt-4o"}


def test_default_always_present_in_empty_case() -> None:
    s = _settings(LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-sonnet-4-5")
    assert s.default_model() in s.allowed_models()


# --------------------------- non-empty allowlist WITHOUT default → prepended first ---------
def test_allowlist_without_default_prepends_default_first() -> None:
    raw = json.dumps({"claude-haiku": "Claude Haiku", "claude-opus": "Claude Opus"})
    s = _settings(
        LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-sonnet-4-5", ANTHROPIC_MODELS=raw
    )
    result = s.allowed_models()
    # default prepended FIRST (displayName = id), then allowlist insertion order preserved.
    assert list(result.keys()) == ["claude-sonnet-4-5", "claude-haiku", "claude-opus"]
    assert result["claude-sonnet-4-5"] == "claude-sonnet-4-5"
    assert result["claude-haiku"] == "Claude Haiku"
    assert s.default_model() in result


# --------------------------- non-empty allowlist WITH default → as-is ---------------------------
def test_allowlist_with_default_returned_as_is_order_preserved() -> None:
    raw = json.dumps(
        {
            "claude-sonnet-4-5": "Claude Sonnet 4.5",
            "claude-haiku": "Claude Haiku",
        }
    )
    s = _settings(
        LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-sonnet-4-5", ANTHROPIC_MODELS=raw
    )
    result = s.allowed_models()
    assert list(result.keys()) == ["claude-sonnet-4-5", "claude-haiku"]
    # no duplicate default, displayName from allowlist kept.
    assert result["claude-sonnet-4-5"] == "Claude Sonnet 4.5"


# --------------------------- shape rules (token_products parity) ---------------------------
def test_non_str_values_are_dropped() -> None:
    # value must be a non-empty str; ints / null / nested / bool / empty-str are dropped.
    raw = json.dumps(
        {
            "good": "Good Model",
            "as_int": 5,
            "as_null": None,
            "as_bool": True,
            "as_obj": {"x": 1},
            "empty_val": "",
        }
    )
    s = _settings(LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-def", ANTHROPIC_MODELS=raw)
    result = s.allowed_models()
    # only "good" survives, default prepended.
    assert set(result.keys()) == {"claude-def", "good"}
    assert result["good"] == "Good Model"


def test_blank_keys_are_dropped_and_keys_stripped() -> None:
    raw = json.dumps({"   ": "blank-key", "  spaced  ": "Spaced"})
    s = _settings(LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-def", ANTHROPIC_MODELS=raw)
    result = s.allowed_models()
    # blank key dropped; the spaced key is stripped to "spaced".
    assert "   " not in result
    assert "spaced" in result
    assert result["spaced"] == "Spaced"


def test_malformed_json_yields_empty_then_default_fallback() -> None:
    s = _settings(
        LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-def", ANTHROPIC_MODELS="not-json{{{"
    )
    assert s.allowed_models() == {"claude-def": "claude-def"}


def test_non_object_json_yields_default_fallback() -> None:
    # A JSON array (not an object) → empty → backward-compatible default fallback.
    s = _settings(
        LLM_PROVIDER="anthropic", ANTHROPIC_MODEL="claude-def", ANTHROPIC_MODELS='["a","b"]'
    )
    assert s.allowed_models() == {"claude-def": "claude-def"}


# --------------------------- provider selection of the raw allowlist ---------------------------
def test_provider_selects_anthropic_raw_when_anthropic() -> None:
    anth = json.dumps({"claude-x": "Claude X"})
    open = json.dumps({"gpt-x": "GPT X"})
    s = _settings(
        LLM_PROVIDER="anthropic",
        ANTHROPIC_MODEL="claude-def",
        ANTHROPIC_MODELS=anth,
        OPENAI_MODELS=open,
    )
    result = s.allowed_models()
    assert "claude-x" in result
    assert "gpt-x" not in result  # the openai raw is NOT read on an anthropic instance


def test_provider_selects_openai_raw_when_openai() -> None:
    anth = json.dumps({"claude-x": "Claude X"})
    open = json.dumps({"gpt-x": "GPT X"})
    s = _settings(
        LLM_PROVIDER="openai",
        OPENAI_MODEL="gpt-def",
        ANTHROPIC_MODELS=anth,
        OPENAI_MODELS=open,
    )
    result = s.allowed_models()
    assert "gpt-x" in result
    assert "claude-x" not in result
    assert s.default_model() == "gpt-def"
    assert s.default_model() in result
