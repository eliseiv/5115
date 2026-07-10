"""Signed preview URLs (HMAC-SHA256 + TTL) — ADR-010, website-builder/05-security.md.

token = base64url(exp) . base64url(HMAC_SHA256(PREVIEW_URL_SECRET, "projectId|ownerUserId|exp"))

- Secret is the isolated PREVIEW_URL_SECRET (stdlib hmac/hashlib; no new dependency).
- Verification is constant-time (hmac.compare_digest) and checks TTL together with the HMAC.
- The signature binds projectId AND ownerUserId — forging access to another project is
  impossible (changing any field breaks the HMAC).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass

from app.config import get_settings


class PreviewSecretMissingError(RuntimeError):
    """PREVIEW_URL_SECRET is not configured — preview cannot be issued/verified safely."""


@dataclass(frozen=True)
class SignedPreview:
    token: str
    expires_at: int  # unix ts


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _secret() -> bytes:
    secret = get_settings().preview_url_secret
    if not secret:
        raise PreviewSecretMissingError("PREVIEW_URL_SECRET is not configured")
    return secret.encode("utf-8")


def _canonical(project_id: uuid.UUID, owner_user_id: uuid.UUID, exp: int) -> bytes:
    return f"{project_id}|{owner_user_id}|{exp}".encode()


def _sign(project_id: uuid.UUID, owner_user_id: uuid.UUID, exp: int) -> bytes:
    return hmac.new(_secret(), _canonical(project_id, owner_user_id, exp), hashlib.sha256).digest()


def build_token(
    *, project_id: uuid.UUID, owner_user_id: uuid.UUID, now: int | None = None
) -> SignedPreview:
    """Build a signed token for a project owned by owner_user_id, valid for the configured TTL."""
    issued = now if now is not None else int(time.time())
    exp = issued + get_settings().preview_url_ttl_seconds
    mac = _sign(project_id, owner_user_id, exp)
    token = f"{_b64url_encode(str(exp).encode('ascii'))}.{_b64url_encode(mac)}"
    return SignedPreview(token=token, expires_at=exp)


def verify_token(
    *,
    project_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    token: str,
    now: int | None = None,
) -> bool:
    """Verify a preview token: HMAC (constant-time) + TTL. Returns False on any mismatch/expiry.

    The signature is recomputed over (projectId, ownerUserId, exp) and compared constant-time.
    Tampering with projectId/ownerUserId/exp breaks the HMAC. An expired exp fails even if the
    HMAC matches. Malformed tokens return False (never raise to the caller).
    """
    current = now if now is not None else int(time.time())
    parts = token.split(".")
    if len(parts) != 2:
        return False
    exp_part, mac_part = parts
    try:
        exp = int(_b64url_decode(exp_part).decode("ascii"))
        presented_mac = _b64url_decode(mac_part)
    except (ValueError, UnicodeDecodeError):
        return False

    expected_mac = _sign(project_id, owner_user_id, exp)
    # Always compute compare_digest before the TTL check so timing does not reveal which failed.
    mac_ok = hmac.compare_digest(presented_mac, expected_mac)
    if not mac_ok:
        return False
    return current <= exp
