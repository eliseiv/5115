"""BYOK service: envelope encryption set/toggle/delete + key retrieval (ADR-003, byok/03).

Plaintext key and plaintext DEK are NEVER persisted or logged. get_plaintext_key returns
the key in-memory only to Chat Orchestrator on the time of an Anthropic call; the caller
zeroizes after use.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_BYOK_CHANGE, AuditEvent, AuditService
from app.byok.kms import KmsClient
from app.byok.provider_detect import detect_byok_provider

# AnthropicClient is imported for backward compatibility (conftest patches
# ``byok.service.AnthropicClient``); the service depends on the neutral LLMClient (ADR-033).
from app.chat.anthropic_client import AnthropicClient  # noqa: F401
from app.chat.llm_client import KeyValidation, LLMClient, llm_client_for
from app.config import get_settings
from app.models import BYOKKey

_DEK_LEN = 32
_NONCE_LEN = 12


@dataclass(frozen=True)
class BYOKResult:
    byok_enabled: bool
    # ADR-016: valid | invalid | missing | validating | offline | expired.
    key_status: str
    # ADR-016: active model when key_status == valid, else None.
    active_model: str | None = None


def _active_model_for(key_status: str, provider: str | None) -> str | None:
    """Active model reported only when the key is valid (ADR-016), per STORED provider (ADR-044 §6).

    The BYOK default model depends on the KEY's provider (NOT ``LLM_PROVIDER``): ``anthropic`` →
    ``BYOK_DEFAULT_MODEL``; ``openai`` → ``OPENAI_BYOK_DEFAULT_MODEL``. ``provider is None`` is a
    legacy row (pre-migration 0013) whose provider was never recorded and is too expensive to detect
    on a read (would require decrypting the key) → fall back to the active instance default (the
    pre-ADR-044 behavior). This degradation affects only legacy rows until their next ``set``, which
    writes the fresh provider (ADR-044 §6, TD-029).
    """
    if key_status != "valid":
        return None
    settings = get_settings()
    if provider is None:
        # Legacy row: provider unknown without decrypting the key → active-instance default.
        return settings.byok_default_model_for(settings.llm_provider.strip().lower())
    return settings.byok_default_model_for(provider)


class BYOKService:
    def __init__(
        self,
        session: AsyncSession,
        kms: KmsClient,
        anthropic_client: LLMClient,
        audit: AuditService,
    ) -> None:
        self._session = session
        self._kms = kms
        # ADR-033: provider-neutral LLM client (the param name is kept for caller compatibility).
        self._anthropic = anthropic_client
        self._audit = audit

    async def _load(self, user_id: uuid.UUID) -> BYOKKey | None:
        row: BYOKKey | None = await self._session.scalar(
            select(BYOKKey).where(BYOKKey.user_id == user_id)
        )
        return row

    async def set_key(self, user_id: uuid.UUID, api_key: str) -> BYOKResult:
        """Encrypt and store a user key (envelope encryption). Validates the key (ADR-016/ADR-044).

        Multi-provider (ADR-044 §1,§3): the provider is DETECTED from the key prefix, NOT from
        ``LLM_PROVIDER``. An unrecognized format → terminal ``invalid`` WITHOUT any network call
        (nothing to validate against). Otherwise the key is validated via the DETECTED provider's
        client (``llm_client_for``) and the detected provider is stored in ``byok_keys.provider``.

        Status transitions: missing → validating → (valid | invalid | offline). 401 → invalid;
        network/non-401 → offline; success → valid (+ activeModel of the detected provider). An
        invalid/offline key is still stored encrypted with its status; byok is never auto-enabled
        when not valid.
        """
        # ADR-044 §1,§3.1: detect the provider from the key prefix (pure, never logs the key). An
        # unknown format → terminal invalid with NO network call (we do not probe foreign providers
        # with arbitrary input); the key is still stored encrypted (provider=NULL, enabled=False).
        provider = detect_byok_provider(api_key)
        if provider is None:
            return await self._store_key(
                user_id=user_id,
                api_key=api_key,
                key_status="invalid",
                provider=None,
            )

        # ADR-044 §3.2: validate via the DETECTED provider's client (NOT the active instance one).
        validation = await llm_client_for(provider).validate_key(api_key)
        key_status = {
            KeyValidation.valid: "valid",
            KeyValidation.invalid: "invalid",
            KeyValidation.offline: "offline",
        }[validation]
        return await self._store_key(
            user_id=user_id,
            api_key=api_key,
            key_status=key_status,
            provider=provider,
        )

    async def _store_key(
        self,
        *,
        user_id: uuid.UUID,
        api_key: str,
        key_status: str,
        provider: str | None,
    ) -> BYOKResult:
        """Envelope-encrypt and persist the key with its status + detected provider (ADR-003/044).

        Encryption is unchanged (ADR-003: DEK → AES-GCM → KMS-wrap). The detected ``provider`` is
        stored regardless of the validation outcome (ADR-044 §3.3) — it is determined by the key
        format, not by whether the provider accepted the key. ``activeModel`` is the detected
        provider's BYOK default, reported only when valid (ADR-044 §3.4 / §6).
        """
        dek = os.urandom(_DEK_LEN)
        nonce = os.urandom(_NONCE_LEN)
        try:
            aead = AESGCM(dek)
            encrypted_key = aead.encrypt(nonce, api_key.encode("utf-8"), None)
            encrypted_dek = self._kms.encrypt_dek(dek)
        finally:
            # Best-effort zeroization of the plaintext DEK reference.
            dek = b"\x00" * _DEK_LEN

        existing = await self._load(user_id)
        if existing is None:
            row = BYOKKey(
                user_id=user_id,
                encrypted_key=encrypted_key,
                encrypted_dek=encrypted_dek,
                nonce=nonce,
                key_status=key_status,
                enabled=False,
                # ADR-044 §4: store the detected provider (NULL for an unrecognized format).
                provider=provider,
            )
            self._session.add(row)
        else:
            existing.encrypted_key = encrypted_key
            existing.encrypted_dek = encrypted_dek
            existing.nonce = nonce
            existing.key_status = key_status
            # ADR-044 §4: refresh the detected provider on every (re)set (incl. an unknown format
            # overwriting a previously known one → NULL, consistent with the freshly-stored key).
            existing.provider = provider
            # An invalid (re)set must not leave byok enabled.
            if key_status != "valid":
                existing.enabled = False
        await self._session.flush()

        enabled = existing.enabled if existing is not None else False
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_BYOK_CHANGE,
                payload={"action": "set", "keyStatus": key_status, "byokEnabled": enabled},
            )
        )
        return BYOKResult(
            byok_enabled=enabled,
            key_status=key_status,
            active_model=_active_model_for(key_status, provider),
        )

    async def toggle(self, user_id: uuid.UUID, enabled: bool) -> BYOKResult:
        """Enable/disable BYOK. Cannot enable unless key_status == valid (byok/02, ADR-016).

        Extended statuses validating/offline/expired are NOT valid → enabling is rejected.
        """
        row = await self._load(user_id)
        if row is None:
            return BYOKResult(byok_enabled=False, key_status="missing")

        if enabled and row.key_status != "valid":
            # Do not enable; return current status without error (documented default).
            await self._audit.record(
                AuditEvent(
                    user_id=user_id,
                    event_type=EVENT_BYOK_CHANGE,
                    payload={
                        "action": "toggle_rejected",
                        "requested": enabled,
                        "keyStatus": row.key_status,
                    },
                )
            )
            return BYOKResult(
                byok_enabled=False,
                key_status=row.key_status,
                active_model=_active_model_for(row.key_status, row.provider),
            )

        row.enabled = enabled
        await self._session.flush()
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_BYOK_CHANGE,
                payload={"action": "toggle", "byokEnabled": enabled, "keyStatus": row.key_status},
            )
        )
        return BYOKResult(
            byok_enabled=enabled,
            key_status=row.key_status,
            active_model=_active_model_for(row.key_status, row.provider),
        )

    async def delete_key(self, user_id: uuid.UUID) -> BYOKResult:
        """Physically delete the encrypted materials → keyStatus=missing."""
        await self._session.execute(delete(BYOKKey).where(BYOKKey.user_id == user_id))
        await self._session.flush()
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_BYOK_CHANGE,
                payload={"action": "delete", "byokEnabled": False, "keyStatus": "missing"},
            )
        )
        return BYOKResult(byok_enabled=False, key_status="missing", active_model=None)

    async def get_status(self, user_id: uuid.UUID) -> BYOKResult:
        row = await self._load(user_id)
        if row is None:
            return BYOKResult(byok_enabled=False, key_status="missing")
        return BYOKResult(
            byok_enabled=row.enabled,
            key_status=row.key_status,
            active_model=_active_model_for(row.key_status, row.provider),
        )

    async def get_plaintext_key_with_provider(
        self, user_id: uuid.UUID
    ) -> tuple[str, str | None] | None:
        """Decrypt the key in-memory AND resolve its provider for the Orchestrator (ADR-044 §5).

        Returns ``(plaintext_key, provider)`` or ``None`` when no key is stored. The provider is
        read from ``byok_keys.provider`` WITHOUT a second decryption; for a legacy ``provider=NULL``
        row it is detected on the fly from the decrypted plaintext (``detect_byok_provider`` —
        fallback, ADR-044 §4). The returned provider may still be ``None`` if the legacy key's
        format is unrecognized (the caller treats that as a defensive ``byok_invalid`` block — it is
        not reachable for a ``valid`` key). The key is never logged; the caller must not persist it.
        """
        row = await self._load(user_id)
        if row is None:
            return None
        dek = self._kms.decrypt_dek(bytes(row.encrypted_dek))
        try:
            aead = AESGCM(dek)
            plaintext = aead.decrypt(bytes(row.nonce), bytes(row.encrypted_key), None)
        finally:
            dek = b"\x00" * _DEK_LEN
        key = plaintext.decode("utf-8")
        # Prefer the stored provider (no key inspection); legacy NULL → detect from the plaintext.
        provider = row.provider if row.provider is not None else detect_byok_provider(key)
        return key, provider

    async def get_plaintext_key(self, user_id: uuid.UUID) -> str | None:
        """Decrypt the user's key in-memory for Chat Orchestrator. Never logged.

        Returns None if no key stored. Caller must not persist the returned value.
        """
        row = await self._load(user_id)
        if row is None:
            return None
        dek = self._kms.decrypt_dek(bytes(row.encrypted_dek))
        try:
            aead = AESGCM(dek)
            plaintext = aead.decrypt(bytes(row.nonce), bytes(row.encrypted_key), None)
        finally:
            dek = b"\x00" * _DEK_LEN
        return plaintext.decode("utf-8")

    async def mark_invalid(self, user_id: uuid.UUID) -> None:
        """Mark a key invalid at runtime (Anthropic 401) → next policy yields byok_invalid.

        Retained for callers that explicitly want the ``invalid`` terminal state. Per ADR-016,
        the /chat/run runtime 401 path now uses :meth:`mark_expired` (a previously-valid key that
        stopped working is ``expired``, not freshly ``invalid``). Both are non-valid → policy
        yields ``byok_invalid`` either way (ADR-016 §Consequences).
        """
        row = await self._load(user_id)
        if row is None:
            return
        row.key_status = "invalid"
        row.enabled = False
        await self._session.flush()
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_BYOK_CHANGE,
                payload={"action": "runtime_invalidate", "keyStatus": "invalid"},
            )
        )

    async def mark_expired(self, user_id: uuid.UUID) -> None:
        """Mark a previously-valid key expired at runtime (Anthropic 401 on use, ADR-016).

        The key was ``valid`` but Anthropic now rejects it (revoked/expired). Sets status to
        ``expired`` and disables byok; the next policy-evaluate yields ``byok_invalid`` (expired
        is non-valid). A network error on use does NOT call this (transient — status unchanged).
        """
        row = await self._load(user_id)
        if row is None:
            return
        row.key_status = "expired"
        row.enabled = False
        await self._session.flush()
        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_BYOK_CHANGE,
                payload={"action": "runtime_expire", "keyStatus": "expired"},
            )
        )
