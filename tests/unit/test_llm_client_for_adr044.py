"""Unit tests for ``llm_client_for`` — explicit-provider client factory (ADR-044 §2).

``llm_client_for(provider)`` returns the client for an EXPLICIT provider, independent of
``LLM_PROVIDER``; an unknown provider → ``ValueError``. ``get_llm_client()`` keeps its exact
signature/behavior (reads ``LLM_PROVIDER``) and now delegates to ``llm_client_for`` so the shared
process-wide singletons (and conftest patches of them) keep overriding both paths.
"""

from __future__ import annotations

import pytest

import app.chat.anthropic_client as anthropic_mod
import app.chat.llm_client as llm_mod
from app.chat.anthropic_client import AnthropicClient
from app.chat.llm_client import get_llm_client, llm_client_for
from app.chat.openai_client import OpenAIClient
from app.config import get_settings


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts from clean singletons so the factory constructs deterministically."""
    monkeypatch.setattr(anthropic_mod, "_anthropic_singleton", None)
    monkeypatch.setattr(llm_mod, "_openai_singleton", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_llm_client_for_anthropic() -> None:
    client = llm_client_for("anthropic")
    assert isinstance(client, AnthropicClient)


def test_llm_client_for_openai() -> None:
    client = llm_client_for("openai")
    assert isinstance(client, OpenAIClient)


def test_llm_client_for_is_singleton_per_provider() -> None:
    """Both clients are process-wide singletons (same instance on repeat calls)."""
    assert llm_client_for("openai") is llm_client_for("openai")
    assert llm_client_for("anthropic") is llm_client_for("anthropic")


def test_llm_client_for_normalizes_case_and_whitespace() -> None:
    """Provider is normalized via strip().lower() before dispatch (ADR-044 §2)."""
    assert isinstance(llm_client_for("  OpenAI  "), OpenAIClient)
    assert isinstance(llm_client_for("ANTHROPIC"), AnthropicClient)


@pytest.mark.parametrize("bad", ["", "gemini", "mistral", "unknown", "sk-ant-"])
def test_llm_client_for_unknown_raises_value_error(bad: str) -> None:
    """An unknown provider is an internal caller error → ValueError (ADR-044 §2)."""
    with pytest.raises(ValueError, match="unknown LLM provider"):
        llm_client_for(bad)


def test_get_llm_client_unchanged_anthropic_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (no LLM_PROVIDER / anthropic) → AnthropicClient, behavior unchanged (ADR-033 §8)."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    get_settings.cache_clear()
    assert isinstance(get_llm_client(), AnthropicClient)


def test_get_llm_client_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    assert isinstance(get_llm_client(), OpenAIClient)


def test_get_llm_client_delegates_to_factory_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_llm_client() reuses the SAME singletons llm_client_for hands out (single source)."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    assert get_llm_client() is llm_client_for("openai")

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    get_settings.cache_clear()
    assert get_llm_client() is llm_client_for("anthropic")


def test_anthropic_singleton_patch_overrides_both_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """A conftest-style patch of the anthropic singleton overrides factory AND get_llm_client."""

    class _Sentinel:
        pass

    sentinel = _Sentinel()
    monkeypatch.setattr(anthropic_mod, "_anthropic_singleton", sentinel)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    get_settings.cache_clear()
    assert llm_client_for("anthropic") is sentinel
    assert get_llm_client() is sentinel
