"""Integration tests for BYOK encryption-at-rest + audit (AC-5/AC-7, ADR-003)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import app.byok.service as byok_service
from app.audit.service import AuditService
from app.byok.kms import get_kms_client
from app.byok.service import BYOKService
from app.chat.anthropic_client import KeyValidation
from tests.conftest import seed_user


class _FakeAnthropic:
    """Fake Anthropic client honoring the ADR-016 contract: validate_key returns a
    KeyValidation enum (valid|invalid|offline), NOT a bool. Production BYOKService.set_key maps
    the enum to a key_status; returning a bool would KeyError on the enum-keyed mapping."""

    def __init__(self, outcome: KeyValidation) -> None:
        self._outcome = outcome

    async def validate_key(self, api_key: str) -> KeyValidation:
        return self._outcome


# ADR-044 §3.2: set_key validates via the module-level ``llm_client_for(provider)`` (the provider
# DETECTED from the key prefix), NOT the constructor-injected client. So the per-test outcome must
# be installed at THAT seam. A mutable holder lets each _svc(...) pin the outcome the patched
# factory returns (all keys here are sk-ant- → detector routes to "anthropic"). Without this patch
# the real AnthropicClient would hit the network and a fake sk-ant-x would validate as invalid.
_OUTCOME_HOLDER: dict[str, KeyValidation] = {"outcome": KeyValidation.valid}


@pytest.fixture(autouse=True)
def _patch_llm_client_for(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_llm_client_for(provider: str) -> _FakeAnthropic:
        return _FakeAnthropic(_OUTCOME_HOLDER["outcome"])

    monkeypatch.setattr(byok_service, "llm_client_for", _fake_llm_client_for)


def _svc(
    session: AsyncSession,
    *,
    valid: bool = True,
    outcome: KeyValidation | None = None,
) -> BYOKService:
    resolved = (
        outcome
        if outcome is not None
        else (KeyValidation.valid if valid else KeyValidation.invalid)
    )
    # Pin the outcome the patched ``llm_client_for`` (the real validation seam) will return.
    _OUTCOME_HOLDER["outcome"] = resolved
    return BYOKService(session, get_kms_client(), _FakeAnthropic(resolved), AuditService(session))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_key_stored_encrypted_and_round_trips(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session)
    plaintext = "sk-ant-secret-user-key-123"
    await _svc(db_session, valid=True).set_key(uid, plaintext)
    await db_session.commit()

    # Stored ciphertext must NOT equal the plaintext (encrypted-at-rest).
    enc = await db_session.scalar(
        text("SELECT encrypted_key FROM byok_keys WHERE user_id=:u"), {"u": str(uid)}
    )
    assert enc is not None
    assert bytes(enc) != plaintext.encode("utf-8")
    assert plaintext.encode("utf-8") not in bytes(enc)

    # Decrypt round-trip recovers the original key.
    recovered = await _svc(db_session).get_plaintext_key(uid)
    assert recovered == plaintext


@pytest.mark.asyncio
async def test_set_valid_then_toggle_enables(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session)
    r = await _svc(db_session, valid=True).set_key(uid, "sk-ant-x")
    await db_session.commit()
    assert r.key_status == "valid"
    assert r.byok_enabled is False  # not enabled until toggled

    t = await _svc(db_session).toggle(uid, True)
    await db_session.commit()
    assert t.byok_enabled is True
    assert t.key_status == "valid"


@pytest.mark.asyncio
async def test_cannot_enable_invalid_key(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session)
    await _svc(db_session, valid=False).set_key(uid, "sk-ant-bad")
    await db_session.commit()
    t = await _svc(db_session).toggle(uid, True)
    await db_session.commit()
    assert t.byok_enabled is False
    assert t.key_status == "invalid"


@pytest.mark.asyncio
async def test_set_offline_validation_marks_offline_not_enabled(db_session: AsyncSession) -> None:
    """ADR-016: a network/non-401 validation failure → keyStatus=offline, not enabled, no model."""
    uid = await seed_user(db_session)
    r = await _svc(db_session, outcome=KeyValidation.offline).set_key(uid, "sk-ant-x")
    await db_session.commit()
    assert r.key_status == "offline"
    assert r.byok_enabled is False
    assert r.active_model is None
    # offline is non-valid → toggle must not enable.
    t = await _svc(db_session).toggle(uid, True)
    await db_session.commit()
    assert t.byok_enabled is False
    assert t.key_status == "offline"


@pytest.mark.asyncio
async def test_set_valid_reports_active_model(db_session: AsyncSession) -> None:
    """ADR-016: activeModel is reported only when keyStatus == valid."""
    uid = await seed_user(db_session)
    r = await _svc(db_session, valid=True).set_key(uid, "sk-ant-x")
    await db_session.commit()
    assert r.key_status == "valid"
    assert r.active_model is not None


@pytest.mark.asyncio
async def test_runtime_expire_marks_expired_and_blocks(db_session: AsyncSession) -> None:
    """ADR-016: a runtime 401 on a previously-valid key → mark_expired → keyStatus=expired,
    byok disabled (next policy yields byok_invalid)."""
    uid = await seed_user(db_session)
    await _svc(db_session, valid=True).set_key(uid, "sk-ant-x")
    await _svc(db_session).toggle(uid, True)
    await db_session.commit()

    await _svc(db_session).mark_expired(uid)
    await db_session.commit()
    status = await _svc(db_session).get_status(uid)
    assert status.key_status == "expired"
    assert status.byok_enabled is False
    assert status.active_model is None


@pytest.mark.asyncio
async def test_audit_byok_change_records_key_status(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session)
    await _svc(db_session, valid=True).set_key(uid, "sk-ant-x")
    await db_session.commit()
    rows = list(
        await db_session.scalars(
            text("SELECT payload FROM audit_logs WHERE user_id=:u AND event_type='byok_change'"),
            {"u": str(uid)},
        )
    )
    assert rows
    payload = rows[0]
    assert payload["keyStatus"] == "valid"
    # The raw key must never appear in audit payload (AC-7).
    assert "sk-ant-x" not in str(payload)


@pytest.mark.asyncio
async def test_audit_never_contains_plaintext_key(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session)
    secret = "sk-ant-super-secret-99"
    await _svc(db_session, valid=True).set_key(uid, secret)
    await db_session.commit()
    all_payloads = await db_session.scalar(
        text("SELECT string_agg(payload::text, ' ') FROM audit_logs WHERE user_id=:u"),
        {"u": str(uid)},
    )
    assert secret not in (all_payloads or "")


@pytest.mark.asyncio
async def test_runtime_invalidate_disables(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session)
    await _svc(db_session, valid=True).set_key(uid, "sk-ant-x")
    await _svc(db_session).toggle(uid, True)
    await db_session.commit()

    await _svc(db_session).mark_invalid(uid)
    await db_session.commit()
    status = await _svc(db_session).get_status(uid)
    assert status.key_status == "invalid"
    assert status.byok_enabled is False


@pytest.mark.asyncio
async def test_delete_key_sets_missing(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session)
    await _svc(db_session, valid=True).set_key(uid, "sk-ant-x")
    await db_session.commit()
    r = await _svc(db_session).delete_key(uid)
    await db_session.commit()
    assert r.key_status == "missing"
    cnt = await db_session.scalar(
        text("SELECT count(*) FROM byok_keys WHERE user_id=:u"), {"u": str(uid)}
    )
    assert int(cnt) == 0


@pytest.mark.asyncio
async def test_set_key_idempotent_uses_via_seed(db_session: AsyncSession) -> None:
    # seed helper path: ensure stored-by-seed key also decrypts (sanity for other tests).
    uid = await seed_user(db_session, byok_status="valid", byok_enabled=True)
    recovered = await _svc(db_session).get_plaintext_key(uid)
    assert recovered == "sk-ant-user-key"


@pytest.mark.asyncio
async def test_reset_existing_key_updates_row(db_session: AsyncSession) -> None:
    """Second set_key on an existing user updates the stored row (existing-row branch)."""
    uid = await seed_user(db_session)
    await _svc(db_session, valid=True).set_key(uid, "sk-ant-first")
    await _svc(db_session).toggle(uid, True)
    await db_session.commit()

    # Re-set with an invalid key → must disable and store the new ciphertext.
    r = await _svc(db_session, valid=False).set_key(uid, "sk-ant-second")
    await db_session.commit()
    assert r.key_status == "invalid"
    status = await _svc(db_session).get_status(uid)
    assert status.byok_enabled is False
    assert await _svc(db_session).get_plaintext_key(uid) == "sk-ant-second"


@pytest.mark.asyncio
async def test_status_and_key_missing_for_unknown_user(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session)
    status = await _svc(db_session).get_status(uid)
    assert status.key_status == "missing"
    assert await _svc(db_session).get_plaintext_key(uid) is None
    # toggle on a user with no key row is a no-op returning missing.
    t = await _svc(db_session).toggle(uid, True)
    assert t.key_status == "missing"


@pytest.mark.asyncio
async def test_mark_invalid_noop_when_missing(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session)
    await _svc(db_session).mark_invalid(uid)  # no row → returns without error
    await db_session.commit()
    assert (await _svc(db_session).get_status(uid)).key_status == "missing"
