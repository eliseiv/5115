"""Unit tests for the embedded auth-issuer key plumbing (ADR-018 §3/§7).

Covers the pure, I/O-light pieces of the issuer that do not need the DB:
- ``Settings.resolve_private_key`` / ``resolve_public_key``: both PEM-in-env mechanisms
  (``*_PATH`` file and ``\\n``-escaped string) and the file>string priority.
- ``TokenIssuer``: configured flag (503 gating), self-consistent sign→verify round-trip via the
  existing ``JwtVerifier`` (sub==userId, device_id), and IssuerNotConfiguredError when no key.
- ``build_jwks``: emits only the public-key JWKS contract fields {kty,use,alg,kid,n,e}.
- redaction: the private key / signed token never leak through the log redactor.

No DB / app client here — those live in tests/integration/test_auth_api.py.
"""

from __future__ import annotations

import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from app.api_gateway.auth import JwtVerifier
from app.auth.issuer import IssuerNotConfiguredError, TokenIssuer, build_jwks
from app.config import Settings


def _make_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    public_pem = (
        key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    )
    return private_pem, public_pem


def _settings(**overrides: object) -> Settings:
    """Build a Settings without touching the shared lru_cache (validates from a kwargs dict)."""
    return Settings(**overrides)  # type: ignore[arg-type]


# --- resolve_private_key / resolve_public_key ---
def test_resolve_private_key_from_escaped_string() -> None:
    private_pem, _ = _make_keypair()
    escaped = private_pem.replace("\n", "\\n")
    s = _settings(JWT_PRIVATE_KEY=escaped, JWT_PRIVATE_KEY_PATH="")
    assert s.resolve_private_key() == private_pem


def test_resolve_private_key_from_file_path(tmp_path) -> None:
    private_pem, _ = _make_keypair()
    pem_file = tmp_path / "private.pem"
    pem_file.write_text(private_pem, encoding="utf-8")
    s = _settings(JWT_PRIVATE_KEY="", JWT_PRIVATE_KEY_PATH=str(pem_file))
    assert s.resolve_private_key() == private_pem


def test_resolve_private_key_path_takes_priority_over_string(tmp_path) -> None:
    # ADR-018 §7: *_PATH wins over the \n-escaped string when both are set.
    file_pem, _ = _make_keypair()
    string_pem, _ = _make_keypair()
    assert file_pem != string_pem
    pem_file = tmp_path / "private.pem"
    pem_file.write_text(file_pem, encoding="utf-8")
    s = _settings(
        JWT_PRIVATE_KEY=string_pem.replace("\n", "\\n"), JWT_PRIVATE_KEY_PATH=str(pem_file)
    )
    assert s.resolve_private_key() == file_pem


def test_resolve_private_key_empty_when_unconfigured() -> None:
    s = _settings(JWT_PRIVATE_KEY="", JWT_PRIVATE_KEY_PATH="")
    assert s.resolve_private_key() == ""


def test_resolve_public_key_both_mechanisms_work(tmp_path) -> None:
    _, public_pem = _make_keypair()
    s_string = _settings(JWT_PUBLIC_KEY=public_pem.replace("\n", "\\n"), JWT_PUBLIC_KEY_PATH="")
    assert s_string.resolve_public_key() == public_pem
    pem_file = tmp_path / "public.pem"
    pem_file.write_text(public_pem, encoding="utf-8")
    s_file = _settings(JWT_PUBLIC_KEY="", JWT_PUBLIC_KEY_PATH=str(pem_file))
    assert s_file.resolve_public_key() == public_pem


# ----------------------------- TokenIssuer configured flag -----------------------------
def test_issuer_not_configured_without_private_key() -> None:
    issuer = TokenIssuer(_settings(JWT_PRIVATE_KEY="", JWT_PRIVATE_KEY_PATH=""))
    assert issuer.configured is False
    with pytest.raises(IssuerNotConfiguredError):
        issuer.issue_access_token(user_id=uuid.uuid4(), device_id="dev-1")


def test_issuer_configured_with_private_key() -> None:
    private_pem, _ = _make_keypair()
    issuer = TokenIssuer(_settings(JWT_PRIVATE_KEY=private_pem.replace("\n", "\\n")))
    assert issuer.configured is True
    assert issuer.access_ttl_seconds == 3600


# --- self-consistent sign -> verify (ADR-018 §3) ---
def test_issued_token_round_trips_through_jwt_verifier() -> None:
    private_pem, public_pem = _make_keypair()
    issuer_settings = _settings(
        JWT_PRIVATE_KEY=private_pem.replace("\n", "\\n"),
        JWT_PUBLIC_KEY=public_pem.replace("\n", "\\n"),
        JWT_ISSUER="claude-ios-tests",
        JWT_AUDIENCE="claude-ios-tests",
        JWT_KID="test-kid-1",
        JWT_JWKS_URL="",
    )
    issuer = TokenIssuer(issuer_settings)
    uid = uuid.uuid4()
    token = issuer.issue_access_token(user_id=uid, device_id="dev-xyz")

    # The PEM-string verifier reads its key from the SAME settings instance (self-consistent loop).
    # JwtVerifier imports get_settings by name (`from app.config import get_settings`), so patch the
    # symbol resolved in app.api_gateway.auth, not app.config.
    import app.api_gateway.auth as auth_mod

    real_get_settings = auth_mod.get_settings
    auth_mod.get_settings = lambda: issuer_settings  # type: ignore[assignment]
    try:
        verified = JwtVerifier().verify(token)
    finally:
        auth_mod.get_settings = real_get_settings  # type: ignore[assignment]

    assert verified.user_id == uid
    assert verified.device_id == "dev-xyz"
    # kid is present in the header (key-rotation groundwork).
    assert jwt.get_unverified_header(token)["kid"] == "test-kid-1"


def test_issued_token_carries_iss_aud_when_configured() -> None:
    private_pem, public_pem = _make_keypair()
    issuer = TokenIssuer(
        _settings(
            JWT_PRIVATE_KEY=private_pem.replace("\n", "\\n"),
            JWT_ISSUER="my-iss",
            JWT_AUDIENCE="my-aud",
        )
    )
    token = issuer.issue_access_token(user_id=uuid.uuid4(), device_id="d")
    claims = jwt.decode(token, public_pem, algorithms=["RS256"], audience="my-aud", issuer="my-iss")
    assert claims["iss"] == "my-iss"
    assert claims["aud"] == "my-aud"
    assert "iat" in claims and "exp" in claims


# --- build_jwks (GET /v1/auth/jwks contract) ---
def test_build_jwks_emits_only_public_contract_fields() -> None:
    _, public_pem = _make_keypair()
    doc = build_jwks(public_pem, "kid-7")
    assert list(doc.keys()) == ["keys"]
    keys = doc["keys"]
    assert isinstance(keys, list) and len(keys) == 1
    key = keys[0]
    assert set(key.keys()) == {"kty", "use", "alg", "kid", "n", "e"}
    assert key["kty"] == "RSA"
    assert key["use"] == "sig"
    assert key["alg"] == "RS256"
    assert key["kid"] == "kid-7"
    # No private-material fields ever leak into the JWKS document.
    for forbidden in ("d", "p", "q", "dp", "dq", "qi"):
        assert forbidden not in key


# ----------------------------- private key / token never logged -----------------------------
def test_private_key_and_token_are_redacted() -> None:
    from app.observability.redaction import REDACTED, redact

    private_pem, public_pem = _make_keypair()
    issuer = TokenIssuer(_settings(JWT_PRIVATE_KEY=private_pem.replace("\n", "\\n")))
    token = issuer.issue_access_token(user_id=uuid.uuid4(), device_id="d")
    out = redact(
        {
            "jwt_private_key": private_pem,
            "accessToken": token,
            "refreshToken": "opaque-secret-value",
        }
    )
    assert out["jwt_private_key"] == REDACTED
    assert out["accessToken"] == REDACTED
    assert out["refreshToken"] == REDACTED
