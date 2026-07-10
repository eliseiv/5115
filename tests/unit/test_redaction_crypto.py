"""Unit tests for secret redaction (AC-7) and KMS envelope crypto (AC-4/ADR-003)."""

from __future__ import annotations

import base64
import os

import pytest

from app.byok.kms import LocalKmsClient
from app.observability.redaction import REDACTED, assert_no_secrets, redact


# --- Redaction (logs/audit must never carry secrets) ---
def test_redact_sensitive_keys() -> None:
    payload = {
        "apiKey": "sk-ant-secret",
        "authorization": "Bearer abc",
        "token": "jwt.value",
        "nested": {"secret": "x", "password": "p"},
        "transaction": "jws...",
    }
    out = redact(payload)
    assert out["apiKey"] == REDACTED
    assert out["authorization"] == REDACTED
    assert out["token"] == REDACTED
    assert out["nested"]["secret"] == REDACTED
    assert out["nested"]["password"] == REDACTED
    assert out["transaction"] == REDACTED


def test_redact_keeps_status_metadata() -> None:
    # keyStatus must survive (AC-7 byok_change audit needs valid|invalid|missing).
    out = redact({"keyStatus": "valid", "byokEnabled": True})
    assert out["keyStatus"] == "valid"
    assert out["byokEnabled"] is True


def test_redact_recurses_lists() -> None:
    out = redact({"items": [{"apiKey": "x"}, {"ok": 1}]})
    assert out["items"][0]["apiKey"] == REDACTED
    assert out["items"][1]["ok"] == 1


def test_assert_no_secrets_returns_copy() -> None:
    src = {"apiKey": "x"}
    out = assert_no_secrets(src)
    assert out["apiKey"] == REDACTED
    assert src["apiKey"] == "x"  # original untouched


# --- KMS envelope crypto round-trip ---
def test_kms_dek_round_trip() -> None:
    master = os.urandom(32)
    kms = LocalKmsClient(master)
    dek = os.urandom(32)
    wrapped = kms.encrypt_dek(dek)
    assert wrapped != dek
    assert kms.decrypt_dek(wrapped) == dek


def test_kms_rejects_bad_master_key_length() -> None:
    with pytest.raises(ValueError):
        LocalKmsClient(b"short")


def test_kms_ciphertext_nondeterministic() -> None:
    kms = LocalKmsClient(base64.b64decode("MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="))
    dek = os.urandom(32)
    assert kms.encrypt_dek(dek) != kms.encrypt_dek(dek)  # random nonce
