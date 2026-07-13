"""Unit tests for the real StoreKitVerifier failure modes (AC-10, fails closed),
the env-gated HS256 test branch (TD-007, 09-e2e-testing.md §2), and the
STOREKIT_TRUST_ANY_XCODE_CERT anchoring-bypass flag (ADR-061 / TD-039)."""

from __future__ import annotations

import base64
import datetime
import json
import logging

import jwt as pyjwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from app.config import get_settings
from app.errors import ValidationFailedError
from app.subscription.storekit import _XCODE_TESTING_CERT_CN, StoreKitVerifier


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


# --- STOREKIT_TRUST_ANY_XCODE_CERT: ES256 anchoring-bypass for local Xcode certs (ADR-061) ---
#
# The flag skips BOTH anchoring gates (empty-roots refusal + _verify_chain) ONLY for a leaf whose
# CN == _XCODE_TESTING_CERT_CN, while STILL verifying the leaf ES256 signature. These tests craft
# real ES256/x5c JWS with self-signed EC certs (never a real Apple root) and drive the verifier
# through settings, exactly like the HS256 helpers above.

_XCODE_ORG = "StoreKit Testing in Xcode"  # full subject O=... CN=...  (ADR-061 §2)


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def _self_signed_ec_cert(key: ec.EllipticCurvePrivateKey, subject: x509.Name) -> x509.Certificate:
    """A self-signed EC (SECP256R1) cert with the given subject (== issuer)."""
    now = _now()
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )


def _xcode_subject() -> x509.Name:
    """Subject of a local Xcode StoreKit Testing cert: O=... , CN=StoreKit Testing in Xcode."""
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, _XCODE_ORG),
            x509.NameAttribute(NameOID.COMMON_NAME, _XCODE_TESTING_CERT_CN),
        ]
    )


def _es256_jws(
    subject: x509.Name,
    *,
    sign_key: ec.EllipticCurvePrivateKey,
    cert_key: ec.EllipticCurvePrivateKey | None = None,
    claims: dict | None = None,
) -> str:
    """Build an ES256 x5c JWS. The x5c leaf is self-signed by ``cert_key`` (default ``sign_key``);
    the JWS itself is signed by ``sign_key``. Passing a ``cert_key`` != ``sign_key`` yields a token
    whose embedded leaf public key does NOT match the signing key → invalid signature."""
    cert_signing_key = cert_key or sign_key
    cert = _self_signed_ec_cert(cert_signing_key, subject)
    der_b64 = base64.b64encode(cert.public_bytes(Encoding.DER)).decode()
    now = _now()
    payload = {
        "transactionId": "txn-xcode-1",
        "originalTransactionId": "otxn-xcode-1",
        "productId": "pro.monthly",
        "bundleId": "com.example.app",
        "environment": "Sandbox",
        "expiresDate": int((now + datetime.timedelta(days=30)).timestamp() * 1000),
    }
    if claims:
        payload.update(claims)
    return pyjwt.encode(payload, sign_key, algorithm="ES256", headers={"x5c": [der_b64]})


def _xcode_verifier(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trust: bool,
    root_cert_dir: str = "",
    test_mode: bool = False,
    secret: str = "",
) -> StoreKitVerifier:
    """Construct a verifier reflecting the STOREKIT_TRUST_ANY_XCODE_CERT (+ optional roots /
    HS256 test-mode) env. Mirrors ``_verifier_with``: clear the lru_cache around construction so
    the patched env is read and never leaks into the session-shared Settings cache."""
    monkeypatch.setenv("STOREKIT_TRUST_ANY_XCODE_CERT", "true" if trust else "false")
    monkeypatch.setenv("STOREKIT_TEST_MODE", "true" if test_mode else "false")
    monkeypatch.setenv("STOREKIT_TEST_SECRET", secret)
    monkeypatch.setenv("APPSTORE_BUNDLE_ID", "com.example.app")
    monkeypatch.setenv("APPSTORE_ROOT_CERT_DIR", root_cert_dir)
    get_settings.cache_clear()
    try:
        return StoreKitVerifier()
    finally:
        get_settings.cache_clear()


# --- Scenario 1: flag OFF (default) → behaviour unchanged, no bypass ---


def test_flag_off_xcode_cert_still_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a well-formed self-signed Xcode-CN ES256 token is refused when the flag is false and
    no roots are configured — the flag OFF opens no bypass (ADR-061 §4)."""
    verifier = _xcode_verifier(monkeypatch, trust=False)
    key = ec.generate_private_key(ec.SECP256R1())
    token = _es256_jws(_xcode_subject(), sign_key=key)
    with pytest.raises(ValidationFailedError, match="root certificates not configured"):
        verifier.verify(token)


# --- Scenario 2: flag ON + Xcode CN + empty roots + valid sig → ACCEPTED ---


def test_flag_on_xcode_cn_valid_signature_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """flag ON + leaf CN == Xcode constant + empty roots + valid ES256 leaf signature → both
    anchoring gates skipped, transaction accepted with normalized fields (ADR-061 §3)."""
    verifier = _xcode_verifier(monkeypatch, trust=True)
    key = ec.generate_private_key(ec.SECP256R1())
    token = _es256_jws(_xcode_subject(), sign_key=key)

    txn = verifier.verify(token)

    assert txn.transaction_id == "txn-xcode-1"
    assert txn.original_transaction_id == "otxn-xcode-1"
    assert txn.product_id == "pro.monthly"
    assert txn.environment == "sandbox"  # normalized to lowercase
    assert txn.revoked is False
    assert txn.expires_at is not None


# --- Scenario 3: flag ON + Xcode CN + INVALID signature → still rejected ---


def test_flag_on_xcode_cn_invalid_signature_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """flag ON does NOT disable signature verification: a token signed by a different key than the
    one in the presented x5c leaf → ValidationFailedError('signature invalid') (ADR-061 §3)."""
    verifier = _xcode_verifier(monkeypatch, trust=True)
    sign_key = ec.generate_private_key(ec.SECP256R1())
    other_key = ec.generate_private_key(ec.SECP256R1())
    # x5c carries a cert for other_key, but the JWS is signed by sign_key → mismatch.
    token = _es256_jws(_xcode_subject(), sign_key=sign_key, cert_key=other_key)
    with pytest.raises(ValidationFailedError, match="StoreKit JWS signature invalid"):
        verifier.verify(token)


# --- Scenario 4: flag ON + CN != Xcode + empty roots → normal fail-closed (no bypass) ---


def test_flag_on_non_xcode_cn_empty_roots_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """flag ON but CN != Xcode constant → the real path is NOT weakened: empty roots still raise
    'root certificates not configured' (ADR-061 §6)."""
    verifier = _xcode_verifier(monkeypatch, trust=True)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Something Else")])
    token = _es256_jws(subject, sign_key=key)
    with pytest.raises(ValidationFailedError, match="root certificates not configured"):
        verifier.verify(token)


# --- Scenario 5: flag ON + CN != Xcode + NON-empty roots but not anchored → anchor path works ---


def test_flag_on_non_xcode_cn_unanchored_with_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """flag ON + CN != Xcode + roots configured but leaf not anchored → the normal _verify_chain
    anchoring failure ('not anchored to a trusted root') still fires (ADR-061 §5/§6)."""
    # A trusted root that does NOT sign the leaf.
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Unrelated Root CA")])
    root_cert = _self_signed_ec_cert(root_key, root_subject)
    (tmp_path / "root.pem").write_bytes(root_cert.public_bytes(Encoding.PEM))

    verifier = _xcode_verifier(monkeypatch, trust=True, root_cert_dir=str(tmp_path))
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Something Else")])
    token = _es256_jws(subject, sign_key=leaf_key)
    with pytest.raises(ValidationFailedError, match="not anchored to a trusted root"):
        verifier.verify(token)


# --- Scenario 6: CN missing / multiple CNs → treated as non-match → fail-closed ---


def test_flag_on_missing_cn_treated_as_non_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaf with NO CN (only O=...) at flag ON is a non-match → normal fail-closed path
    (empty roots → 'not configured'), never a bypass (ADR-061 §2)."""
    verifier = _xcode_verifier(monkeypatch, trust=True)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, _XCODE_ORG)])
    token = _es256_jws(subject, sign_key=key)
    with pytest.raises(ValidationFailedError, match="root certificates not configured"):
        verifier.verify(token)


def test_flag_on_multiple_cns_treated_as_non_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaf with TWO CNs (both == Xcode constant) is a non-match (len != 1) → fail-closed
    (ADR-061 §2: missing/multiple CN → non-match)."""
    verifier = _xcode_verifier(monkeypatch, trust=True)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, _XCODE_TESTING_CERT_CN),
            x509.NameAttribute(NameOID.COMMON_NAME, _XCODE_TESTING_CERT_CN),
        ]
    )
    token = _es256_jws(subject, sign_key=key)
    with pytest.raises(ValidationFailedError, match="root certificates not configured"):
        verifier.verify(token)


# --- Scenario 7: HS256 test-mode is orthogonal — the new flag does not touch it ---


def test_hs256_test_mode_unaffected_by_trust_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """With STOREKIT_TRUST_ANY_XCODE_CERT=true AND test-mode on, a valid HS256 token is still
    accepted via the HS256 branch — the flags are orthogonal (ADR-061 §7)."""
    verifier = _xcode_verifier(monkeypatch, trust=True, test_mode=True, secret=_TEST_SECRET)
    txn = verifier.verify(_make_hs256(_TEST_SECRET))
    assert txn.transaction_id == "txn-1"
    assert txn.environment == "sandbox"


# --- Scenario 8: startup WARNING when the flag is enabled (ADR-061 §9) ---


async def _run_lifespan_capturing_app_main_warnings(
    monkeypatch: pytest.MonkeyPatch, *, trust: bool
) -> list[str]:
    """Run app.main.lifespan with the trust flag set to ``trust`` and return the WARNING messages
    emitted on the ``app.main`` logger.

    Hermetic across suite ordering: a dedicated handler is attached DIRECTLY to the ``app.main``
    logger (not caplog, whose root handler is cleared by the real ``configure_logging`` that other
    tests run during their app lifespan). ``configure_logging`` is stubbed to a no-op so the
    lifespan does not reconfigure global logging, and the local logger level is forced to WARNING so
    the record is dispatched regardless of the ambient root level.
    """
    import app.main as main_mod

    monkeypatch.setenv("STOREKIT_TRUST_ANY_XCODE_CERT", "true" if trust else "false")
    get_settings.cache_clear()
    monkeypatch.setattr(main_mod, "configure_logging", lambda *_a, **_k: None)

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    target = logging.getLogger("app.main")
    previous_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.WARNING)
    try:
        app = main_mod.create_app()
        async with main_mod.lifespan(app):
            pass
    finally:
        target.removeHandler(handler)
        target.setLevel(previous_level)
        get_settings.cache_clear()

    return [r.getMessage() for r in records if r.levelno == logging.WARNING]


async def test_lifespan_warns_when_trust_flag_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """lifespan emits a WARNING on app.main when the flag is true. The message is a fixed string —
    no token/payload is interpolated (05-security.md / ADR-061 §9)."""
    messages = await _run_lifespan_capturing_app_main_warnings(monkeypatch, trust=True)
    assert any("STOREKIT_TRUST_ANY_XCODE_CERT is ENABLED" in m for m in messages)
    assert any("MUST be false in production" in m for m in messages)


async def test_lifespan_no_warning_when_trust_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No STOREKIT_TRUST_ANY_XCODE_CERT warning when the flag is false (default posture)."""
    messages = await _run_lifespan_capturing_app_main_warnings(monkeypatch, trust=False)
    assert not any("STOREKIT_TRUST_ANY_XCODE_CERT is ENABLED" in m for m in messages)
