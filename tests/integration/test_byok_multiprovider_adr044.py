"""Integration tests for multi-provider BYOK set_key + storage (ADR-044 §3,§4,§6).

Real PostgreSQL (testcontainers) exercises the ``byok_keys.provider`` column (migration 0013) and
the round-trip of the stored provider. The provider client is FAKED at the ``llm_client_for`` seam
for BOTH providers (no real network: tests pass with placeholder API keys). The key insight under
test: on an OpenAI instance (``LLM_PROVIDER=openai``) an ``sk-ant-`` key is validated via the
ANTHROPIC client (detected from the prefix), stored with ``provider='anthropic'``, and reports the
anthropic BYOK default — independent of ``LLM_PROVIDER``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import app.byok.service as byok_service
from app.audit.service import AuditService
from app.byok.kms import get_kms_client
from app.byok.service import BYOKService, _active_model_for
from app.chat.llm_client import KeyValidation
from app.config import get_settings
from tests.conftest import seed_user


class _RecordingClient:
    """LLMClient double that records the validated key and returns a scripted outcome.

    Verifies the routing target: the test asserts which fake (anthropic vs openai) received the
    key, so a mis-routed validation is caught. validate_key NEVER makes a network call.
    """

    def __init__(self, outcome: KeyValidation) -> None:
        self._outcome = outcome
        self.validated_keys: list[str] = []

    async def validate_key(self, api_key: str) -> KeyValidation:
        self.validated_keys.append(api_key)
        return self._outcome


@pytest.fixture
def _route(monkeypatch: pytest.MonkeyPatch):
    """Patch ``llm_client_for`` in byok.service to route to per-provider recording fakes.

    Returns a dict {provider: _RecordingClient}; the default outcome is ``valid`` and can be
    overridden per-provider via the returned ``set_outcome`` callable.
    """
    clients: dict[str, _RecordingClient] = {
        "anthropic": _RecordingClient(KeyValidation.valid),
        "openai": _RecordingClient(KeyValidation.valid),
    }

    def _fake_llm_client_for(provider: str) -> _RecordingClient:
        return clients[provider.strip().lower()]

    monkeypatch.setattr(byok_service, "llm_client_for", _fake_llm_client_for)
    return clients


def _svc(session: AsyncSession) -> BYOKService:
    # The injected anthropic_client arg is unused by set_key (it routes via llm_client_for);
    # pass a harmless recording stub for constructor compatibility.
    return BYOKService(
        session,
        get_kms_client(),
        _RecordingClient(KeyValidation.valid),  # type: ignore[arg-type]
        AuditService(session),
    )


async def _stored_provider(session: AsyncSession, uid: Any) -> str | None:
    return await session.scalar(
        text("SELECT provider FROM byok_keys WHERE user_id=:u"), {"u": str(uid)}
    )


@pytest.fixture
def _openai_instance(monkeypatch: pytest.MonkeyPatch):
    """Force LLM_PROVIDER=openai for the duration of a test (clears settings cache)."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------------------------
# §3.2 — set_key on an OpenAI instance with an sk-ant- key: validated via the ANTHROPIC client.
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_set_ant_key_on_openai_instance_validates_via_anthropic(
    db_session: AsyncSession, _route: dict[str, _RecordingClient], _openai_instance: None
) -> None:
    uid = await seed_user(db_session)
    ant_key = "sk-ant-api03-user-byok-key"
    r = await _svc(db_session).set_key(uid, ant_key)
    await db_session.commit()

    # Routed to the ANTHROPIC fake, NOT the openai one (detected from the sk-ant- prefix).
    assert _route["anthropic"].validated_keys == [ant_key]
    assert _route["openai"].validated_keys == []

    assert r.key_status == "valid"
    # activeModel is the ANTHROPIC BYOK default, independent of LLM_PROVIDER=openai.
    assert r.active_model == get_settings().byok_default_model
    assert r.active_model == get_settings().byok_default_model_for("anthropic")
    # provider='anthropic' persisted in byok_keys (migration 0013 column).
    assert await _stored_provider(db_session, uid) == "anthropic"


@pytest.mark.asyncio
async def test_set_openai_key_on_openai_instance_uses_openai_default(
    db_session: AsyncSession, _route: dict[str, _RecordingClient], _openai_instance: None
) -> None:
    uid = await seed_user(db_session)
    r = await _svc(db_session).set_key(uid, "sk-proj-user-openai-key")
    await db_session.commit()

    assert _route["openai"].validated_keys == ["sk-proj-user-openai-key"]
    assert _route["anthropic"].validated_keys == []
    assert r.key_status == "valid"
    assert r.active_model == get_settings().openai_byok_default_model
    assert await _stored_provider(db_session, uid) == "openai"


# ---------------------------------------------------------------------------------------------
# §3.1 — unrecognized format → terminal invalid WITHOUT any network call; provider stays NULL.
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unrecognized_key_invalid_without_network_call(
    db_session: AsyncSession, _route: dict[str, _RecordingClient]
) -> None:
    uid = await seed_user(db_session)
    r = await _svc(db_session).set_key(uid, "totally-not-a-key")
    await db_session.commit()

    # NEITHER provider client was called — no probing of foreign providers (ADR-044 §3.1/§8).
    assert _route["anthropic"].validated_keys == []
    assert _route["openai"].validated_keys == []

    assert r.key_status == "invalid"
    assert r.byok_enabled is False
    assert r.active_model is None
    # provider persisted as NULL (format not recognized) but the key is still stored encrypted.
    assert await _stored_provider(db_session, uid) is None
    enc = await db_session.scalar(
        text("SELECT encrypted_key FROM byok_keys WHERE user_id=:u"), {"u": str(uid)}
    )
    assert enc is not None  # stored encrypted even though invalid


@pytest.mark.asyncio
async def test_offline_anthropic_key_stores_provider_anthropic(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """provider is stored from the DETECTED prefix regardless of validation outcome (§3.3)."""
    offline_anthropic = _RecordingClient(KeyValidation.offline)
    monkeypatch.setattr(
        byok_service,
        "llm_client_for",
        lambda provider: {"anthropic": offline_anthropic}[provider.strip().lower()],
    )
    uid = await seed_user(db_session)
    r = await _svc(db_session).set_key(uid, "sk-ant-offline-net-error")
    await db_session.commit()
    assert r.key_status == "offline"
    assert r.active_model is None  # not valid → no model
    # Provider is recorded from the prefix even though validation could not complete.
    assert await _stored_provider(db_session, uid) == "anthropic"


# ---------------------------------------------------------------------------------------------
# §4 — reset overwrites the stored provider (anthropic → openai).
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reset_overwrites_stored_provider(
    db_session: AsyncSession, _route: dict[str, _RecordingClient]
) -> None:
    uid = await seed_user(db_session)
    await _svc(db_session).set_key(uid, "sk-ant-first")
    await db_session.commit()
    assert await _stored_provider(db_session, uid) == "anthropic"

    # Re-set with an OpenAI key → provider column flips to openai on the existing row.
    await _svc(db_session).set_key(uid, "sk-proj-second")
    await db_session.commit()
    assert await _stored_provider(db_session, uid) == "openai"


@pytest.mark.asyncio
async def test_reset_to_unrecognized_clears_provider_to_null(
    db_session: AsyncSession, _route: dict[str, _RecordingClient]
) -> None:
    uid = await seed_user(db_session)
    await _svc(db_session).set_key(uid, "sk-ant-first")
    await db_session.commit()
    assert await _stored_provider(db_session, uid) == "anthropic"

    # Re-set with a garbage key → provider NULL (consistent with the freshly-stored key) + invalid.
    r = await _svc(db_session).set_key(uid, "garbage")
    await db_session.commit()
    assert r.key_status == "invalid"
    assert await _stored_provider(db_session, uid) is None


# ---------------------------------------------------------------------------------------------
# §5 / §6 — get_plaintext_key_with_provider: stored provider, with legacy NULL fallback-detect.
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_plaintext_key_with_provider_uses_stored_provider(
    db_session: AsyncSession, _route: dict[str, _RecordingClient]
) -> None:
    uid = await seed_user(db_session)
    await _svc(db_session).set_key(uid, "sk-ant-stored")
    await db_session.commit()
    resolved = await _svc(db_session).get_plaintext_key_with_provider(uid)
    assert resolved == ("sk-ant-stored", "anthropic")


@pytest.mark.asyncio
async def test_legacy_null_provider_fallback_detect_from_plaintext(
    db_session: AsyncSession,
) -> None:
    """A legacy row (provider IS NULL, seeded) resolves the provider by detecting the plaintext."""
    # seed_user stores an "sk-ant-user-key" with provider left NULL (legacy pre-0013 row shape).
    uid = await seed_user(db_session, byok_status="valid", byok_enabled=True)
    assert await _stored_provider(db_session, uid) is None  # legacy NULL

    resolved = await _svc(db_session).get_plaintext_key_with_provider(uid)
    assert resolved is not None
    key, provider = resolved
    assert key == "sk-ant-user-key"
    assert provider == "anthropic"  # detected on the fly (fallback)


@pytest.mark.asyncio
async def test_get_plaintext_key_with_provider_missing_user(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session)
    assert await _svc(db_session).get_plaintext_key_with_provider(uid) is None


# ---------------------------------------------------------------------------------------------
# §6 — _active_model_for per stored provider (incl. legacy NULL → active-instance default, TD-029).
# ---------------------------------------------------------------------------------------------
def test_active_model_for_anthropic() -> None:
    assert _active_model_for("valid", "anthropic") == get_settings().byok_default_model


def test_active_model_for_openai() -> None:
    assert _active_model_for("valid", "openai") == get_settings().openai_byok_default_model


def test_active_model_for_non_valid_is_none() -> None:
    for status in ("invalid", "offline", "expired", "validating", "missing"):
        assert _active_model_for(status, "anthropic") is None
        assert _active_model_for(status, None) is None


def test_active_model_for_legacy_null_uses_active_instance_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider=None (legacy) → the ACTIVE instance default (TD-029), here forced to openai."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    try:
        assert _active_model_for("valid", None) == get_settings().openai_byok_default_model
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------------------------
# §8 — backward compatibility: anthropic instance + sk-ant- key behaves exactly as before.
# ---------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_backward_compat_anthropic_instance_ant_key(
    db_session: AsyncSession, _route: dict[str, _RecordingClient]
) -> None:
    """Default instance (anthropic) + sk-ant- key → anthropic client, anthropic default model."""
    # default LLM_PROVIDER (anthropic); no _openai_instance fixture.
    uid = await seed_user(db_session)
    r = await _svc(db_session).set_key(uid, "sk-ant-classic")
    await db_session.commit()
    assert _route["anthropic"].validated_keys == ["sk-ant-classic"]
    assert r.key_status == "valid"
    assert r.active_model == get_settings().byok_default_model
    assert await _stored_provider(db_session, uid) == "anthropic"
