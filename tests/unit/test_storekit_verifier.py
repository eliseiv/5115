"""Unit tests for the real StoreKitVerifier failure modes (AC-10, fails closed)
and the env-gated HS256 test branch (TD-007, 09-e2e-testing.md §2)."""

from __future__ import annotations

import base64
import datetime
import json

import jwt as pyjwt
import pytest

from app.config import get_settings
from app.errors import ValidationFailedError
from app.subscription.storekit import StoreKitVerifier


@pytest.fixture
def verifier() -> StoreKitVerifier:
    # No APPSTORE_ROOT_CERT_DIR configured in tests → no trust anchor (fails closed).
    return StoreKitVerifier()


def test_non_jws_string_rejected(verifier: StoreKitVerifier) -> None:
    with pytest.raises(ValidationFailedError, match="compact JWS"):
        verifier.verify("not-a-jws")


def test_jws_without_x5c_rejected(verifier: StoreKitVerifier) -> None:
    # header without x5c, two more segments → reaches chain loading.
    import base64
    import json

    header = base64.urlsafe_b64encode(json.dumps({"alg": "ES256"}).encode()).rstrip(b"=").decode()
    forged = f"{header}.{header}.{header}"
    with pytest.raises(ValidationFailedError, match="x5c"):
        verifier.verify(forged)


def test_fails_closed_without_trust_anchor(verifier: StoreKitVerifier) -> None:
    """A syntactically valid chain still fails when no Apple root is configured."""
    import base64
    import json

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    import datetime

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(tz=datetime.UTC))
        .not_valid_after(datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    from cryptography.hazmat.primitives.serialization import Encoding

    der_b64 = base64.b64encode(cert.public_bytes(Encoding.DER)).decode()
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "ES256", "x5c": [der_b64]}).encode())
        .rstrip(b"=")
        .decode()
    )
    payload = (
        base64.urlsafe_b64encode(json.dumps({"transactionId": "1"}).encode()).rstrip(b"=").decode()
    )
    forged = f"{header}.{payload}.{header}"
    with pytest.raises(ValidationFailedError):
        verifier.verify(forged)


# --- HS256 test branch (TD-007): env-gated, fail-closed, never weakens real path ---
_TEST_SECRET = "storekit-test-secret-value"  # noqa: S105 - fixture-only HS256 secret


def _make_hs256(secret: str, *, claims: dict | None = None, expired: bool = False) -> str:
    """Build an HS256-signed JWS like a controlled e2e test transaction."""
    now = datetime.datetime.now(tz=datetime.UTC)
    exp = now - datetime.timedelta(hours=1) if expired else now + datetime.timedelta(hours=1)
    payload = {
        "transactionId": "txn-1",
        "originalTransactionId": "otxn-1",
        "productId": "pro.monthly",
        "bundleId": "com.example.app",
        "environment": "Sandbox",
        "expiresDate": int((now + datetime.timedelta(days=30)).timestamp() * 1000),
        "exp": int(exp.timestamp()),
    }
    if claims:
        payload.update(claims)
    return pyjwt.encode(payload, secret, algorithm="HS256")


def _verifier_with(monkeypatch, *, test_mode: bool, secret: str) -> StoreKitVerifier:
    """Construct a verifier whose settings reflect the given STOREKIT_TEST_* env.

    get_settings is lru_cached; clear it around construction so we read the patched env
    and do not leak a polluted Settings into the rest of the (session-shared) cache.
    """
    monkeypatch.setenv("STOREKIT_TEST_MODE", "true" if test_mode else "false")
    monkeypatch.setenv("STOREKIT_TEST_SECRET", secret)
    monkeypatch.setenv("APPSTORE_BUNDLE_ID", "com.example.app")
    get_settings.cache_clear()
    try:
        return StoreKitVerifier()
    finally:
        get_settings.cache_clear()


def test_hs256_valid_under_secret_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """HS256 signed with the configured secret in test-mode → verified, same normalization."""
    verifier = _verifier_with(monkeypatch, test_mode=True, secret=_TEST_SECRET)
    txn = verifier.verify(_make_hs256(_TEST_SECRET))
    assert txn.transaction_id == "txn-1"
    assert txn.original_transaction_id == "otxn-1"
    assert txn.product_id == "pro.monthly"
    # environment is normalized to lowercase (shared _normalize_payload path).
    assert txn.environment == "sandbox"
    assert txn.revoked is False
    assert txn.expires_at is not None


def test_hs256_rejected_when_test_mode_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a correctly signed HS256 is refused when STOREKIT_TEST_MODE=false (prod posture)."""
    verifier = _verifier_with(monkeypatch, test_mode=False, secret=_TEST_SECRET)
    with pytest.raises(ValidationFailedError, match="signature invalid"):
        verifier.verify(_make_hs256(_TEST_SECRET))


def test_hs256_rejected_when_secret_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """test-mode requires a non-empty secret; flag alone does not enable the HS256 branch."""
    verifier = _verifier_with(monkeypatch, test_mode=True, secret="")
    with pytest.raises(ValidationFailedError, match="signature invalid"):
        verifier.verify(_make_hs256(_TEST_SECRET))


def test_hs256_wrong_secret_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid HS256 signature (wrong secret) → same 422 as a forged real transaction."""
    verifier = _verifier_with(monkeypatch, test_mode=True, secret=_TEST_SECRET)
    with pytest.raises(ValidationFailedError, match="signature invalid"):
        verifier.verify(_make_hs256("a-different-secret"))


def test_hs256_expired_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired HS256 (exp in the past) → ValidationFailedError (422)."""
    verifier = _verifier_with(monkeypatch, test_mode=True, secret=_TEST_SECRET)
    with pytest.raises(ValidationFailedError, match="signature invalid"):
        verifier.verify(_make_hs256(_TEST_SECRET, expired=True))


def test_es256_x5c_uses_real_branch_even_in_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ES256/x5c transaction ALWAYS takes the real path and fails closed without a root CA,
    even when test-mode is enabled (test-mode never weakens the Apple JWS path)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.x509.oid import NameOID

    verifier = _verifier_with(monkeypatch, test_mode=True, secret=_TEST_SECRET)

    # A real, syntactically valid ES256 x5c chain — but no Apple root is configured in tests,
    # so the real branch must fail closed (it must NOT silently accept under test-mode).
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.datetime.now(tz=datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    der_b64 = base64.b64encode(cert.public_bytes(Encoding.DER)).decode()
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "ES256", "x5c": [der_b64]}).encode())
        .rstrip(b"=")
        .decode()
    )
    payload = (
        base64.urlsafe_b64encode(json.dumps({"transactionId": "1"}).encode()).rstrip(b"=").decode()
    )
    forged = f"{header}.{payload}.{header}"
    # Reaches the real-branch trust-anchor check (no root configured) → fail-closed 422,
    # never the HS256 test branch. The error message is the real-path "not configured" one.
    with pytest.raises(ValidationFailedError, match="root certificates not configured"):
        verifier.verify(forged)
