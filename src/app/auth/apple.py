"""Apple identity-token (Sign in with Apple) verification (ADR-043 §2, modules/auth Phase 6).

Verifies an Apple OIDC identity token (native Sign in with Apple) and returns the domain
``VerifiedAppleIdentity``. Modelled on ``JwtVerifier`` (PyJWKClient + cache,
``src/app/api_gateway/auth.py``) and ``StoreKitVerifier``'s alg-branching test-mode
(``src/app/subscription/storekit.py``).

Alg-branching by the JWS header ``alg``:
- ``RS256`` (real Apple token) — ALWAYS the real path: resolve the signing key from Apple JWKS
  (cached for ``jwks_cache_ttl_seconds``) and verify signature/iss/aud/exp + required claims.
- ``HS256`` — test-branch, active ONLY when ``apple_test_mode`` AND a non-empty
  ``apple_test_secret`` (both). HS256 outside test-mode => UnauthorizedError (no alg-confusion).

Every verification failure (bad signature/iss/aud/exp, missing required claim, unresolvable
JWKS key, network error, nonce mismatch) raises ``UnauthorizedError`` (401, fail-closed) with a
generic message. The identity token and nonce are NEVER logged or placed in exception text
(05-security.md; redaction covers ``*token*``/``nonce``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from app.config import get_settings
from app.errors import ServiceUnavailableError, UnauthorizedError


@dataclass(frozen=True)
class VerifiedAppleIdentity:
    """Domain result of a successful Apple identity-token verification (ADR-043 §2)."""

    apple_sub: str
    email: str | None
    email_verified: bool


class AppleIdentityVerifier:
    """Verifies Apple-signed OIDC identity tokens (Sign in with Apple, native flow)."""

    def __init__(self) -> None:
        settings = get_settings()
        self._issuer = settings.apple_oidc_issuer
        self._audience = settings.apple_audience_resolved()
        # test-mode (ADR-043 §2): HS256 path is honored ONLY when both flag and secret are set;
        # never weakens the real RS256 path. Default false => prod unchanged.
        self._test_secret = settings.apple_test_secret
        self._test_mode = settings.apple_test_mode and bool(self._test_secret)
        # PyJWKClient keeps a per-kid cache; lifespan bounds how long a JWKS fetch is reused
        # (reuses the existing jwks_cache_ttl_seconds; no separate env, ADR-043 §3).
        self._jwks_client = PyJWKClient(
            settings.apple_jwks_url,
            cache_keys=True,
            lifespan=settings.jwks_cache_ttl_seconds,
        )

    @property
    def configured(self) -> bool:
        """True iff the Apple audience is configured (else the endpoint must return 503)."""
        return bool(self._audience)

    def verify(self, identity_token: str, nonce: str | None) -> VerifiedAppleIdentity:
        """Verify an Apple identity token and return the domain identity.

        Branch selection is by the JWS header ``alg`` (ADR-043 §2):
        - ``HS256`` => test-branch, ONLY when test-mode is active (flag + secret); otherwise 401.
        - any other alg (RS256) => ALWAYS the real Apple JWKS path (fail-closed).

        Raises ``ServiceUnavailableError`` (503) when the Apple audience is not configured, and
        ``UnauthorizedError`` (401) on ANY verification failure. The token/nonce are never logged
        or embedded in exception messages.
        """
        if not self._audience:
            # Operational misconfiguration (no Apple audience), not a client error (ADR-043 §1).
            raise ServiceUnavailableError("apple sign-in is not configured")

        try:
            header = jwt.get_unverified_header(identity_token)
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("invalid apple identity token") from exc
        alg = str(header.get("alg", ""))

        if alg == "HS256":
            if not self._test_mode:
                # Fail-closed: HS256 is never accepted outside test-mode (no alg-confusion).
                raise UnauthorizedError("invalid apple identity token")
            claims = self._decode_test(identity_token)
        else:
            claims = self._decode_real(identity_token)

        self._check_nonce(claims, nonce)

        sub: Any = claims.get("sub")
        if not sub:
            raise UnauthorizedError("invalid apple identity token")
        return VerifiedAppleIdentity(
            apple_sub=str(sub),
            email=claims.get("email"),
            email_verified=bool(claims.get("email_verified", False)),
        )

    def _decode_real(self, identity_token: str) -> dict[str, Any]:
        """Real Apple-signed RS256 token: JWKS signing key + signature/iss/aud/exp verification."""
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(identity_token).key
        except (jwt.PyJWKClientError, httpx.HTTPError) as exc:
            # JWKS unreachable / key not found => fail-closed (401), not a 5xx (ADR-043 §6).
            raise UnauthorizedError("invalid apple identity token") from exc
        try:
            claims: dict[str, Any] = jwt.decode(
                identity_token,
                key=signing_key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": ["sub", "iss", "aud", "exp"],
                    "verify_aud": True,
                },
            )
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("invalid apple identity token") from exc
        return claims

    def _decode_test(self, identity_token: str) -> dict[str, Any]:
        """test-mode (ADR-043 §2): HS256 token signed with APPLE_TEST_SECRET (hermetic tests).

        Same response semantics as the real path; an invalid HS256 signature / wrong iss/aud /
        expired token raises the same UnauthorizedError (401) as a forged real token.
        """
        try:
            claims: dict[str, Any] = jwt.decode(
                identity_token,
                key=self._test_secret,
                algorithms=["HS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": ["sub", "iss", "aud", "exp"],
                    "verify_aud": True,
                },
            )
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("invalid apple identity token") from exc
        return claims

    @staticmethod
    def _check_nonce(claims: dict[str, Any], nonce: str | None) -> None:
        """Optional nonce check (ADR-043 §2): verify only when BOTH sides are present.

        Apple stores the SHA-256 hex of the raw nonce in the ``nonce`` claim (native flow). When
        the token has a ``nonce`` claim AND the client sent a ``nonce``, require
        ``sha256(nonce).hexdigest() == claim`` (mismatch => 401). When either side is absent, the
        nonce is not checked (optional on MVP; hardening => Q-043-1). Plain string equality (the
        compared values are hashes, not secrets).
        """
        claim_nonce = claims.get("nonce")
        if claim_nonce and nonce:
            expected = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
            if expected != str(claim_nonce):
                raise UnauthorizedError("invalid apple identity token")


_verifier_singleton: AppleIdentityVerifier | None = None


def get_apple_verifier() -> AppleIdentityVerifier:
    global _verifier_singleton
    if _verifier_singleton is None:
        _verifier_singleton = AppleIdentityVerifier()
    return _verifier_singleton
