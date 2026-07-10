"""AuthService: device-based find-or-create identity + access/refresh issuance (ADR-018).

- find-or-create identity by deviceId (auth_devices); deviceId optional => generate UUIDv4.
- eager users provisioning (INSERT ... ON CONFLICT DO NOTHING — same idempotent upsert as
  get_current_user / ADR-007); lazy provisioning stays a fallback.
- access token via TokenIssuer (RS256); refresh token opaque (secrets.token_urlsafe), stored
  ONLY as sha256(token_hash), single-use rotation, reuse-detect => revoke the device chain.

Concurrency: a race between two register calls for the same deviceId is resolved by
``ON CONFLICT (device_id) DO NOTHING`` on auth_devices + re-reading the winning row — both
callers converge on one userId (auth/03 §Find-or-create). The opaque refresh token and the
signed access token are never logged (05-security.md).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.apple import AppleIdentityVerifier
from app.auth.issuer import IssuerNotConfiguredError, TokenIssuer
from app.config import Settings
from app.errors import ServiceUnavailableError, UnauthorizedError


@dataclass(frozen=True)
class IssuedTokens:
    """Result of a successful auth operation (register/token/refresh)."""

    user_id: uuid.UUID
    device_id: str
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_in: int


def _hash_refresh(token: str) -> str:
    """sha256 hex of the opaque refresh token. The plaintext is never persisted/logged."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        issuer: TokenIssuer,
        settings: Settings,
        apple_verifier: AppleIdentityVerifier,
    ) -> None:
        self._session = session
        self._issuer = issuer
        self._refresh_ttl = settings.auth_refresh_ttl_seconds
        # ADR-043: Apple identity-token verifier (Sign in with Apple). Injected so the audience /
        # JWKS client / test-mode resolution stays a process-wide singleton.
        self._apple = apple_verifier

    def _require_issuer(self) -> None:
        if not self._issuer.configured:
            raise ServiceUnavailableError("auth issuer is not configured")

    async def _find_or_create_identity(self, device_id: str) -> uuid.UUID:
        """Resolve userId for deviceId, creating users + auth_devices for a new device.

        Idempotent and race-safe: ``users`` upsert (ADR-007) and ``auth_devices`` insert both use
        ``ON CONFLICT DO NOTHING``; after a conflicting insert we re-read to take the winning row.
        """
        existing = await self._session.execute(
            text("SELECT user_id FROM auth_devices WHERE device_id = :device_id"),
            {"device_id": device_id},
        )
        row = existing.first()
        if row is not None:
            await self._session.execute(
                text("UPDATE auth_devices SET last_seen_at = now() WHERE device_id = :device_id"),
                {"device_id": device_id},
            )
            return uuid.UUID(str(row[0]))

        new_user_id = uuid.uuid4()
        # Eager provisioning (ADR-018 §4) — same idempotent upsert as the gateway lazy path.
        await self._session.execute(
            text("INSERT INTO users (id) VALUES (:id) ON CONFLICT (id) DO NOTHING"),
            {"id": str(new_user_id)},
        )
        await self._session.execute(
            text(
                "INSERT INTO auth_devices (user_id, device_id) VALUES (:user_id, :device_id) "
                "ON CONFLICT (device_id) DO NOTHING"
            ),
            {"user_id": str(new_user_id), "device_id": device_id},
        )
        # Re-read to take the winning userId (handles the concurrent-register race).
        resolved = await self._session.execute(
            text("SELECT user_id FROM auth_devices WHERE device_id = :device_id"),
            {"device_id": device_id},
        )
        winner = resolved.first()
        if winner is None:  # pragma: no cover - the insert above guarantees a row
            raise ServiceUnavailableError("failed to provision device identity")
        return uuid.UUID(str(winner[0]))

    async def _issue_pair(self, user_id: uuid.UUID, device_id: str) -> IssuedTokens:
        """Issue an access JWT + a fresh opaque refresh token (stored hashed)."""
        try:
            access_token = self._issuer.issue_access_token(user_id=user_id, device_id=device_id)
        except IssuerNotConfiguredError as exc:
            raise ServiceUnavailableError("auth issuer is not configured") from exc

        refresh_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=self._refresh_ttl)
        await self._session.execute(
            text(
                "INSERT INTO auth_refresh_tokens (user_id, device_id, token_hash, expires_at) "
                "VALUES (:user_id, :device_id, :token_hash, :expires_at)"
            ),
            {
                "user_id": str(user_id),
                "device_id": device_id,
                "token_hash": _hash_refresh(refresh_token),
                "expires_at": expires_at,
            },
        )
        return IssuedTokens(
            user_id=user_id,
            device_id=device_id,
            access_token=access_token,
            expires_in=self._issuer.access_ttl_seconds,
            refresh_token=refresh_token,
            refresh_expires_in=self._refresh_ttl,
        )

    async def register_or_token(self, device_id: str | None) -> IssuedTokens:
        """find-or-create identity for deviceId and issue a token pair (register/token).

        deviceId is optional: when absent/empty a UUIDv4 is generated and returned to the client.
        register and token share this path (auth/02): they differ only in whether the client must
        supply deviceId, enforced at the schema layer.
        """
        self._require_issuer()
        resolved_device_id = device_id or str(uuid.uuid4())
        user_id = await self._find_or_create_identity(resolved_device_id)
        tokens = await self._issue_pair(user_id, resolved_device_id)
        await self._session.commit()
        return tokens

    async def sign_in_with_apple(
        self, identity_token: str, device_id: str | None, nonce: str | None
    ) -> IssuedTokens:
        """Sign in with Apple: verify the identity token and issue OUR token pair (ADR-043 §5).

        One atomic flow (single request transaction, idempotent, race-safe), yielding the SAME
        TokenResponse contract as register/token. Cross-device account: one ``apple_sub`` =>
        one ``userId`` on every device.

        Steps:
        1. Require OUR issuer (else 503, like register_or_token); verify the Apple token (the
           verifier raises 503 when the Apple audience is not configured, 401 on any bad token).
        2. Resolve deviceId (None => generate a UUIDv4, returned to the client).
        3. Look up ``auth_identities (provider='apple', subject=apple_sub)``:
           - found => target = its userId (cross-device, same account).
           - not found => resolve the device's userId (find-or-create); if that user has NO Apple
             identity yet, LINK to it (preserves the anonymous device account's credits/history);
             otherwise create a NEW user and link the Apple identity to it.
        4. Upsert the device binding to the resulting user (conflict apple_sub-user != device-user
           => the device is re-pointed to the apple_sub-user; no data auto-merge, Q-043-2).
        5. Issue OUR pair and commit.
        """
        self._require_issuer()
        # Verifier owns the audience "not configured" => 503 decision (ADR-043 §2 single place).
        identity = self._apple.verify(identity_token, nonce)
        apple_sub = identity.apple_sub
        resolved_device_id = device_id or str(uuid.uuid4())

        target_user_id = await self._resolve_apple_user(
            apple_sub=apple_sub, email=identity.email, device_id=resolved_device_id
        )
        await self._upsert_device(resolved_device_id, target_user_id)

        tokens = await self._issue_pair(target_user_id, resolved_device_id)
        await self._session.commit()
        return tokens

    async def _resolve_apple_user(
        self, *, apple_sub: str, email: str | None, device_id: str
    ) -> uuid.UUID:
        """Resolve the userId an Apple identity maps to, linking/creating as needed (ADR-043 §5).

        Idempotent and race-safe: the Apple-identity INSERT uses ``ON CONFLICT (provider, subject)
        DO NOTHING`` and we re-read the winning row, so a concurrent first sign-in of the same
        ``apple_sub`` converges on one userId (no duplicates). ``email`` is written only when the
        identity row is created; existing rows are never overwritten (Apple sends email only on
        first consent — Q-043-1).
        """
        existing = await self._session.execute(
            text(
                "SELECT user_id FROM auth_identities "
                "WHERE provider = 'apple' AND subject = :subject"
            ),
            {"subject": apple_sub},
        )
        row = existing.first()
        if row is not None:
            return uuid.UUID(str(row[0]))

        # Unknown Apple identity. Resolve the device's user (create users+auth_devices if new).
        device_user_id = await self._find_or_create_identity(device_id)

        has_apple = await self._session.execute(
            text(
                "SELECT 1 FROM auth_identities "
                "WHERE user_id = :user_id AND provider = 'apple' LIMIT 1"
            ),
            {"user_id": str(device_user_id)},
        )
        if has_apple.first() is None:
            # Device account has no Apple identity yet => link to it (keep credits/history).
            link_to = device_user_id
        else:
            # Device account already owns a different Apple identity => create a new user.
            link_to = uuid.uuid4()
            await self._session.execute(
                text("INSERT INTO users (id) VALUES (:id) ON CONFLICT (id) DO NOTHING"),
                {"id": str(link_to)},
            )

        await self._session.execute(
            text(
                "INSERT INTO auth_identities (user_id, provider, subject, email) "
                "VALUES (:user_id, 'apple', :subject, :email) "
                "ON CONFLICT (provider, subject) DO NOTHING"
            ),
            {"user_id": str(link_to), "subject": apple_sub, "email": email},
        )
        # Re-read to take the winning userId (handles a concurrent first sign-in of this apple_sub).
        resolved = await self._session.execute(
            text(
                "SELECT user_id FROM auth_identities "
                "WHERE provider = 'apple' AND subject = :subject"
            ),
            {"subject": apple_sub},
        )
        winner = resolved.first()
        if winner is None:  # pragma: no cover - the insert above guarantees a row
            raise ServiceUnavailableError("failed to link apple identity")
        return uuid.UUID(str(winner[0]))

    async def _upsert_device(self, device_id: str, user_id: uuid.UUID) -> None:
        """Bind the device to ``user_id`` (ADR-043 §5 step 6), race-safe via ON CONFLICT.

        On a conflict (the device previously belonged to another userId, e.g. signing into one's
        Apple account on a shared device) the apple_sub-user wins: the device is re-pointed to it
        and ``last_seen_at`` refreshed. No data auto-merge of the prior device account (Q-043-2).
        """
        await self._session.execute(
            text(
                "INSERT INTO auth_devices (user_id, device_id) VALUES (:user_id, :device_id) "
                "ON CONFLICT (device_id) DO UPDATE "
                "SET user_id = EXCLUDED.user_id, last_seen_at = now()"
            ),
            {"user_id": str(user_id), "device_id": device_id},
        )

    async def refresh(self, refresh_token: str) -> IssuedTokens:
        """Rotate a refresh token into a new pair (single-use). Reuse/invalid => 401.

        On presenting a token whose ``used_at`` is already set (reuse), the entire device chain is
        revoked (theft detection, auth/03 §Refresh) and 401 is returned. Unknown/expired/revoked
        tokens also yield 401 without revealing which.
        """
        self._require_issuer()
        token_hash = _hash_refresh(refresh_token)
        result = await self._session.execute(
            text(
                "SELECT id, user_id, device_id, expires_at, used_at, revoked_at "
                "FROM auth_refresh_tokens WHERE token_hash = :token_hash"
            ),
            {"token_hash": token_hash},
        )
        row = result.mappings().first()
        if row is None:
            raise UnauthorizedError("invalid refresh token")

        user_id = uuid.UUID(str(row["user_id"]))
        device_id = str(row["device_id"])

        # Reuse-detect: a used token presented again => theft. Revoke the whole device chain.
        if row["used_at"] is not None:
            await self._revoke_chain(user_id, device_id)
            await self._session.commit()
            raise UnauthorizedError("refresh token reuse detected")

        if row["revoked_at"] is not None:
            raise UnauthorizedError("refresh token revoked")

        expires_at = row["expires_at"]
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise UnauthorizedError("refresh token expired")

        # Single-use: mark the presented token used, then issue a new pair (rotation).
        await self._session.execute(
            text("UPDATE auth_refresh_tokens SET used_at = now() WHERE id = :id"),
            {"id": str(row["id"])},
        )
        tokens = await self._issue_pair(user_id, device_id)
        await self._session.commit()
        return tokens

    async def _revoke_chain(self, user_id: uuid.UUID, device_id: str) -> None:
        await self._session.execute(
            text(
                "UPDATE auth_refresh_tokens SET revoked_at = now() "
                "WHERE user_id = :user_id AND device_id = :device_id AND revoked_at IS NULL"
            ),
            {"user_id": str(user_id), "device_id": device_id},
        )
