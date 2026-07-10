"""Unit: AppleIdentityVerifier — test-mode HS256 branch, fail-closed mapping (ADR-043 §2).

Hermetic: NO network to Apple. Only the HS256 test-branch is exercised (active iff
APPLE_TEST_MODE=true AND a non-empty APPLE_TEST_SECRET). The real RS256 path is NOT hit
(PyJWKClient is never asked to fetch JWKS — every RS256 token is rejected before any network
call because, with a forged signature/unresolvable kid, key resolution fails-closed to 401, and
we never feed a genuinely Apple-signed token). Every verification failure maps to
UnauthorizedError (401); a missing Apple audience maps to ServiceUnavailableError (503). The
identity token / nonce are never embedded in exception text.

The verifier reads get_settings() at construction; we build a throwaway Settings via model_copy
and patch app.auth.apple.get_settings so a fresh AppleIdentityVerifier() picks it up. No DB here.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
import pytest

from app.config import get_settings
from app.errors import ServiceUnavailableError, UnauthorizedError

_TEST_SECRET = "apple-hs256-test-secret"  # noqa: S105 (test-only HS256 secret)
_APPLE_ISSUER = "https://appleid.apple.com"
_APPLE_AUD = "com.example.bundle"


def _verifier(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Any:
    """Build an AppleIdentityVerifier whose Settings are overridden for this test.

    Default posture: test-mode ON, secret set, issuer + audience configured. Override any field
    via kwargs (e.g. apple_test_mode=False, apple_audience="").
    """
    from app.auth import apple as apple_mod

    base = get_settings()
    update: dict[str, Any] = {
        "apple_test_mode": True,
        "apple_test_secret": _TEST_SECRET,
        "apple_oidc_issuer": _APPLE_ISSUER,
        "apple_audience": _APPLE_AUD,
        # Ensure the fallback (appstore_bundle_id) does not silently configure the audience when a
        # test explicitly clears apple_audience to assert the 503 "not configured" path.
        "appstore_bundle_id": overrides.pop("appstore_bundle_id", _APPLE_AUD),
    }
    update.update(overrides)
    configured = base.model_copy(update=update)
    monkeypatch.setattr(apple_mod, "get_settings", lambda: configured)
    return apple_mod.AppleIdentityVerifier()


def _hs256(
    *,
    secret: str = _TEST_SECRET,
    sub: str = "001234.apple.subject",
    iss: str = _APPLE_ISSUER,
    aud: str = _APPLE_AUD,
    expired: bool = False,
    extra: dict[str, Any] | None = None,
    drop: set[str] | None = None,
) -> str:
    now = datetime.now(UTC)
    exp = now - timedelta(hours=1) if expired else now + timedelta(hours=1)
    claims: dict[str, Any] = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    if extra:
        claims.update(extra)
    for key in drop or set():
        claims.pop(key, None)
    return pyjwt.encode(claims, secret, algorithm="HS256")


# ----------------------------- happy path: valid HS256 test token -----------------------------
def test_test_mode_valid_hs256_returns_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    v = _verifier(monkeypatch)
    token = _hs256(extra={"email": "a@b.com", "email_verified": True})
    identity = v.verify(token, None)
    assert identity.apple_sub == "001234.apple.subject"
    assert identity.email == "a@b.com"
    assert identity.email_verified is True


def test_test_mode_email_absent_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    v = _verifier(monkeypatch)
    identity = v.verify(_hs256(), None)
    assert identity.email is None
    assert identity.email_verified is False


# ----------------------------- fail-closed: bad signature / exp / iss / aud -> 401 -------------
def test_invalid_signature_401(monkeypatch: pytest.MonkeyPatch) -> None:
    v = _verifier(monkeypatch)
    forged = _hs256(secret="wrong-secret")
    with pytest.raises(UnauthorizedError):
        v.verify(forged, None)


def test_expired_token_401(monkeypatch: pytest.MonkeyPatch) -> None:
    v = _verifier(monkeypatch)
    with pytest.raises(UnauthorizedError):
        v.verify(_hs256(expired=True), None)


def test_wrong_issuer_401(monkeypatch: pytest.MonkeyPatch) -> None:
    v = _verifier(monkeypatch)
    with pytest.raises(UnauthorizedError):
        v.verify(_hs256(iss="https://evil.example"), None)


def test_wrong_audience_401(monkeypatch: pytest.MonkeyPatch) -> None:
    v = _verifier(monkeypatch)
    with pytest.raises(UnauthorizedError):
        v.verify(_hs256(aud="com.someone.else"), None)


@pytest.mark.parametrize("missing", ["sub", "iss", "aud", "exp"])
def test_missing_required_claim_401(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    v = _verifier(monkeypatch)
    with pytest.raises(UnauthorizedError):
        v.verify(_hs256(drop={missing}), None)


def test_malformed_token_401(monkeypatch: pytest.MonkeyPatch) -> None:
    v = _verifier(monkeypatch)
    with pytest.raises(UnauthorizedError):
        v.verify("not-a-jwt", None)


# ----------------------------- alg-confusion guard: HS256 only in test-mode --------------------
def test_hs256_rejected_when_test_mode_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # APPLE_TEST_MODE=false => the HS256 branch is closed: a valid-by-secret HS256 token is 401
    # (no alg-confusion). The real RS256 path is never weakened by test-mode being on/off.
    v = _verifier(monkeypatch, apple_test_mode=False)
    with pytest.raises(UnauthorizedError):
        v.verify(_hs256(), None)


def test_hs256_rejected_when_secret_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # test-mode is active ONLY when BOTH the flag AND a non-empty secret are set.
    v = _verifier(monkeypatch, apple_test_mode=True, apple_test_secret="")
    with pytest.raises(UnauthorizedError):
        v.verify(_hs256(), None)


# ----------------------------- "not configured" -> 503 ----------------------------------------
def test_audience_not_configured_503(monkeypatch: pytest.MonkeyPatch) -> None:
    # No explicit audience AND no APPSTORE_BUNDLE_ID fallback => "not configured" => 503.
    v = _verifier(monkeypatch, apple_audience="", appstore_bundle_id="")
    assert v.configured is False
    with pytest.raises(ServiceUnavailableError):
        v.verify(_hs256(), None)


def test_audience_falls_back_to_bundle_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty APPLE_AUDIENCE but APPSTORE_BUNDLE_ID set => audience resolves to the bundle id.
    v = _verifier(monkeypatch, apple_audience="", appstore_bundle_id="com.fallback.bundle")
    assert v.configured is True
    identity = v.verify(_hs256(aud="com.fallback.bundle"), None)
    assert identity.apple_sub == "001234.apple.subject"


# ----------------------------- nonce policy (optional, checked when both present) --------------
def test_nonce_match_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    v = _verifier(monkeypatch)
    raw = "raw-nonce-xyz"
    hashed = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    identity = v.verify(_hs256(extra={"nonce": hashed}), raw)
    assert identity.apple_sub == "001234.apple.subject"


def test_nonce_mismatch_401(monkeypatch: pytest.MonkeyPatch) -> None:
    v = _verifier(monkeypatch)
    hashed = hashlib.sha256(b"a-different-nonce").hexdigest()
    with pytest.raises(UnauthorizedError):
        v.verify(_hs256(extra={"nonce": hashed}), "raw-nonce-xyz")


def test_nonce_skipped_when_claim_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Client sent a nonce but the token has no nonce claim => not checked (MVP optional).
    v = _verifier(monkeypatch)
    identity = v.verify(_hs256(), "client-sent-nonce")
    assert identity.apple_sub == "001234.apple.subject"


def test_nonce_skipped_when_request_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Token has a nonce claim but the client sent none => not checked (MVP optional).
    v = _verifier(monkeypatch)
    hashed = hashlib.sha256(b"whatever").hexdigest()
    identity = v.verify(_hs256(extra={"nonce": hashed}), None)
    assert identity.apple_sub == "001234.apple.subject"


# ----------------------------- token/nonce never leak into exception text ----------------------
def test_token_and_nonce_not_in_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    v = _verifier(monkeypatch)
    secret_nonce = "super-secret-nonce-value"
    hashed = hashlib.sha256(b"unmatched").hexdigest()
    token = _hs256(extra={"nonce": hashed})
    with pytest.raises(UnauthorizedError) as exc:
        v.verify(token, secret_nonce)
    msg = str(exc.value)
    assert token not in msg
    assert secret_nonce not in msg
