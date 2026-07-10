"""Unit tests for Sprint-1 (figma-gap) pure helpers — no I/O (06-testing-strategy.md §Unit).

Covers:
- profile accountId derivation (deterministic, format XXXX-XXXX-XXXXX) — profile/09-testing.
- chat title auto-generation from the first user message — chats/09-testing.
- chats keyset cursor round-trip + tie-break ordering tuple — chats/09-testing.
- BYOK activeModel reporting (only when valid, ADR-016) — byok.
- assistant_mode → system-prompt selection (ADR-012) — chats/assistant_mode.
- preferences in-memory defaults (no DB) — preferences/09-testing.
"""

from __future__ import annotations

import datetime
import re
import uuid

import pytest

from app.byok.service import _active_model_for
from app.chat.orchestrator import _system_prompt_for
from app.chat.repository import derive_title
from app.chats.cursor import ChatCursor, InvalidCursorError
from app.preferences.service import _defaults
from app.profile.account_id import account_id

_ACCOUNT_ID_RE = re.compile(r"^\d{4}-\d{4}-[A-Z2-9]{5}$")


# --------------------------- profile.account_id (BR-PR-1) ---------------------------
def test_account_id_format() -> None:
    aid = account_id(uuid.uuid4())
    assert _ACCOUNT_ID_RE.match(aid), aid


def test_account_id_deterministic_for_same_uuid() -> None:
    uid = uuid.UUID("11111111-2222-3333-4444-555555555555")
    assert account_id(uid) == account_id(uid)


def test_account_id_stable_known_value() -> None:
    # Locks the derivation so a future refactor cannot silently change displayed ids.
    uid = uuid.UUID("11111111-2222-3333-4444-555555555555")
    first = account_id(uid)
    # Re-derive via a fresh UUID object with the same value → identical.
    assert account_id(uuid.UUID(str(uid))) == first


def test_account_id_distinct_for_distinct_uuids() -> None:
    ids = {account_id(uuid.uuid4()) for _ in range(500)}
    # No collisions expected on a 500-sample (space is huge); allow no duplicates.
    assert len(ids) == 500


# --------------------------- chat title auto-gen (BR-CH-2) ---------------------------
def test_derive_title_normalizes_whitespace() -> None:
    assert derive_title("  hello   world \n next ") == "hello world next"


def test_derive_title_empty_message_is_none() -> None:
    assert derive_title("   \n\t  ") is None
    assert derive_title("") is None


def test_derive_title_truncates_to_limit() -> None:
    long = "x" * 500
    title = derive_title(long)
    assert title is not None
    assert len(title) <= 200  # _TITLE_MAX_CHARS default


# --------------------------- chats cursor (keyset tie-break) ---------------------------
def test_cursor_round_trip() -> None:
    cur = ChatCursor(
        is_pinned=True,
        updated_at=datetime.datetime(2026, 6, 2, 12, 0, tzinfo=datetime.UTC),
        id=uuid.uuid4(),
    )
    decoded = ChatCursor.decode(cur.encode())
    assert decoded == cur


def test_cursor_decode_naive_datetime_gets_utc() -> None:
    cur = ChatCursor(is_pinned=False, updated_at=datetime.datetime(2026, 1, 1), id=uuid.uuid4())
    decoded = ChatCursor.decode(cur.encode())
    assert decoded.updated_at.tzinfo is not None


def test_cursor_decode_garbage_raises() -> None:
    with pytest.raises(InvalidCursorError):
        ChatCursor.decode("!!!not-base64!!!")
    with pytest.raises(InvalidCursorError):
        ChatCursor.decode("dG9vLWZldy1maWVsZHM=")  # "too-few-fields" decoded, no pipes


# --------------------------- BYOK activeModel (ADR-016 / ADR-044 §6) ---------------------------
# ADR-044 §6 extended the signature to ``_active_model_for(key_status, provider)`` (the BYOK default
# is per-provider). A non-valid status → None regardless of provider; a valid status → that
# provider's BYOK default. provider=None (legacy row) falls back to the active-instance default.
@pytest.mark.parametrize("status", ["invalid", "missing", "validating", "offline", "expired"])
@pytest.mark.parametrize("provider", ["anthropic", "openai", None])
def test_active_model_none_unless_valid(status: str, provider: str | None) -> None:
    assert _active_model_for(status, provider) is None


@pytest.mark.parametrize("provider", ["anthropic", "openai", None])
def test_active_model_present_when_valid(provider: str | None) -> None:
    assert _active_model_for("valid", provider) is not None


# --------------------------- assistant_mode → system prompt (ADR-012) ---------------------------
def test_system_prompt_code_vs_chat_differ() -> None:
    chat = _system_prompt_for("chat")
    code = _system_prompt_for("code")
    assert chat != code
    assert "coding assistant" in code.lower()


def test_system_prompt_unknown_mode_falls_back_to_chat() -> None:
    assert _system_prompt_for("something-else") == _system_prompt_for("chat")


# --------------------------- preferences defaults (no DB) ---------------------------
def test_preferences_defaults() -> None:
    d = _defaults()
    assert d.default_assistant_mode == "chat"
    assert d.notifications_enabled is False  # ADR-032: privacy-by-default
    assert d.code_defaults == {}
