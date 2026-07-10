"""TokenIssuer: RS256 access-token signing for the embedded auth-issuer (ADR-018, auth/03).

Signs access JWTs with the private key resolved from config (file path > \\n-escaped string).
Claims: sub=userId, device_id, iss=JWT_ISSUER, aud=JWT_AUDIENCE, iat, exp; kid in the header.
Verified by the existing JwtVerifier (self-consistent loop) — its logic is unchanged.

The private key and the signed token are NEVER logged (05-security.md, redaction covers *key*).
When no private key is configured the issuer is "unavailable" => endpoints return 503.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from jwt.algorithms import RSAAlgorithm

from app.config import Settings


def build_jwks(public_key_pem: str, kid: str) -> dict[str, object]:
    """Build a JWKS document (one RSA public key) from a PEM public key (auth/02 GET /jwks).

    Returns ``{"keys": [{kty, n, e, use, alg, kid}]}``. Only public material — no private key.
    """
    algorithm = RSAAlgorithm(RSAAlgorithm.SHA256)
    prepared = algorithm.prepare_key(public_key_pem)
    raw: dict[str, object] = json.loads(algorithm.to_jwk(prepared))
    # Emit only the JWKS contract fields (auth/02). PyJWT also adds key_ops, which the strict
    # response schema would reject — take just kty/n/e and the signing metadata.
    key = {
        "kty": raw["kty"],
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": raw["n"],
        "e": raw["e"],
    }
    return {"keys": [key]}


class IssuerNotConfiguredError(Exception):
    """No private signing key is configured (ADR-018 §7) — issuer endpoints must return 503."""


class TokenIssuer:
    """Signs RS256 access tokens for device-based identities (ADR-018 §3)."""

    def __init__(self, settings: Settings) -> None:
        self._private_key = settings.resolve_private_key()
        self._issuer = settings.jwt_issuer or None
        self._audience = settings.jwt_audience or None
        self._kid = settings.jwt_kid or None
        self._access_ttl = settings.auth_access_ttl_seconds

    @property
    def configured(self) -> bool:
        """True iff a private signing key is available (otherwise issuer endpoints 503)."""
        return bool(self._private_key)

    @property
    def access_ttl_seconds(self) -> int:
        return self._access_ttl

    def issue_access_token(self, *, user_id: uuid.UUID, device_id: str) -> str:
        """Sign an access JWT for (userId, deviceId). Raises IssuerNotConfiguredError if no key.

        The returned token is a secret-equivalent credential and is never logged by callers.
        """
        if not self._private_key:
            raise IssuerNotConfiguredError("no private signing key configured")
        now = datetime.now(UTC)
        claims: dict[str, object] = {
            "sub": str(user_id),
            "device_id": device_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self._access_ttl)).timestamp()),
        }
        if self._issuer is not None:
            claims["iss"] = self._issuer
        if self._audience is not None:
            claims["aud"] = self._audience
        headers = {"kid": self._kid} if self._kid is not None else None
        return jwt.encode(claims, self._private_key, algorithm="RS256", headers=headers)
