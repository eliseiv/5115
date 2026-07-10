"""KMS abstraction for envelope encryption (ADR-003, Q-002-1).

Interface `KmsClient(encrypt_dek, decrypt_dek)` is stable regardless of the concrete cloud
provider (Q-002-1 open: AWS KMS / GCP KMS / Azure Key Vault / Vault Transit). The cloud-
backed implementation is selected by config in prod.

LocalKmsClient is a real AES-256-GCM wrap of the DEK under a master key from
KMS_LOCAL_MASTER_KEY, used when no cloud KMS provider is configured (local dev / CI without
cloud access). The concrete cloud provider is deferred per Q-002-1 (interface stable); this
is a config-gated implementation, not a fake — the DEK is never stored in plaintext.
"""

from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

_LOCAL_NONCE_LEN = 12


class KmsClient(ABC):
    """Encrypts/decrypts the data encryption key (DEK) under a KMS master key."""

    @abstractmethod
    def encrypt_dek(self, plaintext_dek: bytes) -> bytes: ...

    @abstractmethod
    def decrypt_dek(self, encrypted_dek: bytes) -> bytes: ...


class LocalKmsClient(KmsClient):
    """AES-256-GCM master-key wrap of the DEK. Non-cloud config-gated impl (Q-002-1)."""

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise ValueError("KMS_LOCAL_MASTER_KEY must decode to exactly 32 bytes")
        self._aead = AESGCM(master_key)

    def encrypt_dek(self, plaintext_dek: bytes) -> bytes:
        nonce = os.urandom(_LOCAL_NONCE_LEN)
        ciphertext = self._aead.encrypt(nonce, plaintext_dek, None)
        return nonce + ciphertext

    def decrypt_dek(self, encrypted_dek: bytes) -> bytes:
        nonce, ciphertext = encrypted_dek[:_LOCAL_NONCE_LEN], encrypted_dek[_LOCAL_NONCE_LEN:]
        return self._aead.decrypt(nonce, ciphertext, None)


_kms_singleton: KmsClient | None = None


def get_kms_client() -> KmsClient:
    """Return the configured KMS client.

    Cloud provider selection (Q-002-1) plugs in here in prod. Until a provider is wired,
    falls back to LocalKmsClient using KMS_LOCAL_MASTER_KEY.
    """
    global _kms_singleton
    if _kms_singleton is not None:
        return _kms_singleton
    settings = get_settings()
    if not settings.kms_local_master_key:
        raise RuntimeError("No KMS provider configured (Q-002-1) and KMS_LOCAL_MASTER_KEY is empty")
    master_key = base64.b64decode(settings.kms_local_master_key)
    _kms_singleton = LocalKmsClient(master_key)
    return _kms_singleton
