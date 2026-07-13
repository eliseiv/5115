"""StoreKit JWS transaction verification (subscription/03, Q-007-1).

Real verification of Apple's signed JWS transaction: the JWS header carries an x5c
certificate chain; we verify the chain up to an Apple root CA (loaded from
APPSTORE_ROOT_CERT_DIR), then verify the JWS signature with the leaf certificate's public
key, then validate the decoded payload (bundle id, environment). StoreKit payload is never
logged (05-security.md).

Apple root CAs are an external trust anchor (Q-007-1 covers sandbox/prod posture). When
APPSTORE_ROOT_CERT_DIR is not configured (local/CI without Apple roots), chain verification
cannot complete, and the verifier refuses to mark a transaction verified (fails closed with
a 422) rather than accepting an unverifiable transaction.
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from app.config import get_settings
from app.errors import ValidationFailedError

logger = logging.getLogger("app.subscription.storekit")

# ADR-061 / TD-039: leaf subject CN of a local Xcode StoreKit Testing self-signed cert
# (full subject `O=StoreKit Testing in Xcode, CN=StoreKit Testing in Xcode`). Exact CN match.
_XCODE_TESTING_CERT_CN = "StoreKit Testing in Xcode"


def _leaf_cn_matches_xcode(leaf: x509.Certificate) -> bool:
    """True only when the leaf has exactly one CN equal to `_XCODE_TESTING_CERT_CN`.

    Missing / multiple CNs, or ANY exception while extracting the CN, are treated as a
    non-match (fail-safe): no positive match => the caller keeps the normal fail-closed path.
    """
    try:
        attributes = leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if len(attributes) != 1:
            return False
        return attributes[0].value == _XCODE_TESTING_CERT_CN
    except Exception:  # noqa: BLE001 - fail-safe: any extraction error => non-match
        return False


@dataclass(frozen=True)
class VerifiedTransaction:
    transaction_id: str
    original_transaction_id: str
    product_id: str
    expires_at: datetime.datetime | None
    revoked: bool
    environment: str


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _jws_header(jws: str) -> dict[str, Any]:
    header_segment = jws.split(".", 1)[0]
    try:
        header = json.loads(_b64url_decode(header_segment))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValidationFailedError("StoreKit JWS header is not valid base64url JSON") from exc
    if not isinstance(header, dict):
        raise ValidationFailedError("StoreKit JWS header must be a JSON object")
    return header


def _load_certificate_chain(jws: str) -> list[x509.Certificate]:
    header = _jws_header(jws)
    x5c = header.get("x5c")
    if not x5c or not isinstance(x5c, list):
        raise ValidationFailedError("StoreKit JWS missing x5c certificate chain")
    return [x509.load_der_x509_certificate(base64.b64decode(cert)) for cert in x5c]


def _verify_chain(chain: list[x509.Certificate], roots: list[x509.Certificate]) -> None:
    """Verify each cert is signed by the next, and the chain terminates in a trusted root."""
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

    def _verify_signed_by(child: x509.Certificate, issuer: x509.Certificate) -> None:
        hash_alg = child.signature_hash_algorithm
        if hash_alg is None:
            raise ValidationFailedError("certificate missing signature hash algorithm")
        pubkey = issuer.public_key()
        if isinstance(pubkey, ec.EllipticCurvePublicKey):
            pubkey.verify(child.signature, child.tbs_certificate_bytes, ec.ECDSA(hash_alg))
        elif isinstance(pubkey, rsa.RSAPublicKey):
            pubkey.verify(
                child.signature,
                child.tbs_certificate_bytes,
                padding.PKCS1v15(),
                hash_alg,
            )
        else:  # pragma: no cover - Apple uses EC; defensive
            raise ValidationFailedError("Unsupported certificate key type in StoreKit chain")

    for i in range(len(chain) - 1):
        _verify_signed_by(chain[i], chain[i + 1])

    root_in_chain = chain[-1]
    root_fingerprints = {r.public_bytes(Encoding.DER) for r in roots}
    if root_in_chain.public_bytes(Encoding.DER) not in root_fingerprints:
        # Verify the chain root is itself signed by a trusted Apple root.
        for trusted in roots:
            try:
                _verify_signed_by(root_in_chain, trusted)
                return
            except Exception:  # noqa: BLE001 - try next trusted root
                continue
        raise ValidationFailedError("StoreKit certificate chain not anchored to a trusted root")


class StoreKitVerifier:
    """Verifies Apple-signed StoreKit JWS transactions."""

    def __init__(self) -> None:
        settings = get_settings()
        self._bundle_id = settings.appstore_bundle_id
        self._environment = settings.appstore_environment
        self._roots = self._load_roots(settings.appstore_root_cert_dir)
        # test-mode: TD-007 (09-e2e-testing.md §2). Active ONLY when both flag and secret set;
        # never weakens the real ES256/x5c path. Default false => prod unchanged.
        self._test_secret = settings.storekit_test_secret
        self._test_mode = settings.storekit_test_mode and bool(self._test_secret)
        # ADR-061 / TD-039: trust any self-signed local Xcode StoreKit Testing cert on the ES256
        # path. Default false => prod fail-closed; real Apple path never weakened. Pre-release
        # test instance only. Startup WARNING is emitted in app.main lifespan (ADR-061 §9).
        self._trust_any_xcode_cert = settings.storekit_trust_any_xcode_cert

    @staticmethod
    def _load_roots(cert_dir: str) -> list[x509.Certificate]:
        if not cert_dir:
            return []
        roots: list[x509.Certificate] = []
        directory = Path(cert_dir)
        if not directory.is_dir():
            return []
        for path in sorted(directory.glob("*")):
            if path.suffix.lower() not in (".cer", ".der", ".pem", ".crt"):
                continue
            data = path.read_bytes()
            try:
                roots.append(x509.load_der_x509_certificate(data))
            except ValueError:
                roots.append(x509.load_pem_x509_certificate(data))
        return roots

    def verify(self, signed_transaction: str) -> VerifiedTransaction:
        """Verify a single signed JWS transaction and return its normalized fields.

        Branch selection is by the JWS header `alg` (09-e2e-testing.md §2.2):
        - `alg=HS256` → test branch, but ONLY when test-mode is active (flag + secret).
          Used in e2e/CI to accept a controlled HS256 transaction signed with
          STOREKIT_TEST_SECRET. # test-mode: TD-007
        - any other alg (ES256 with x5c) → ALWAYS the real Apple JWS path (chain to Apple
          root, fail-closed). The real path is never weakened by test-mode.

        Raises ValidationFailedError on any verification failure (→ technical 422).
        """
        if not isinstance(signed_transaction, str) or signed_transaction.count(".") != 2:
            raise ValidationFailedError("StoreKit transaction must be a compact JWS string")

        header = _jws_header(signed_transaction)
        alg = str(header.get("alg", ""))

        if alg == "HS256":
            # test-mode: TD-007 — HS256 path is only honored when test-mode is enabled.
            if not self._test_mode:
                raise ValidationFailedError("StoreKit JWS signature invalid")
            return self._verify_test_transaction(signed_transaction)

        return self._verify_real_transaction(signed_transaction)

    def _verify_real_transaction(self, signed_transaction: str) -> VerifiedTransaction:
        """Real Apple-signed JWS: x5c chain to Apple root + ES256 leaf signature."""
        chain = _load_certificate_chain(signed_transaction)
        leaf = chain[0]

        # ADR-061 / TD-039: trust a self-signed local Xcode StoreKit Testing cert. ONLY when the
        # flag is on AND the leaf CN matches _XCODE_TESTING_CERT_CN do we skip BOTH anchoring
        # gates (empty-roots refusal and _verify_chain). The leaf ES256 signature below is ALWAYS
        # verified — the flag never disables signature verification. Any other cert (real Apple
        # sandbox/prod, CN mismatch) keeps the full fail-closed path even when the flag is on.
        trust_xcode = self._trust_any_xcode_cert and _leaf_cn_matches_xcode(leaf)

        if not trust_xcode:
            if not self._roots:
                # No trust anchor configured (Q-007-1): cannot complete chain verification.
                raise ValidationFailedError(
                    "App Store root certificates not configured (APPSTORE_ROOT_CERT_DIR); "
                    "cannot verify StoreKit transaction"
                )
            _verify_chain(chain, self._roots)

        leaf_pubkey = leaf.public_key()
        try:
            payload: dict[str, Any] = jwt.decode(
                signed_transaction,
                key=leaf_pubkey,  # type: ignore[arg-type]
                algorithms=["ES256"],
                options={"verify_aud": False},
            )
        except jwt.InvalidTokenError as exc:
            raise ValidationFailedError("StoreKit JWS signature invalid") from exc

        return self._normalize_payload(payload)

    def _verify_test_transaction(self, signed_transaction: str) -> VerifiedTransaction:
        """test-mode: TD-007 — HS256 JWS signed with STOREKIT_TEST_SECRET (e2e/CI only).

        Same response semantics as the real path; an invalid HS256 signature (wrong secret /
        expired) raises the same ValidationFailedError as a forged real transaction (→ 422).
        """
        try:
            payload: dict[str, Any] = jwt.decode(
                signed_transaction,
                key=self._test_secret,
                algorithms=["HS256"],
                options={"verify_aud": False},
            )
        except jwt.InvalidTokenError as exc:
            raise ValidationFailedError("StoreKit JWS signature invalid") from exc

        return self._normalize_payload(payload)

    def _normalize_payload(self, payload: dict[str, Any]) -> VerifiedTransaction:
        """Normalize a verified transaction payload (shared by real and test paths)."""
        bundle_id = payload.get("bundleId")
        if self._bundle_id and bundle_id != self._bundle_id:
            raise ValidationFailedError("StoreKit transaction bundleId mismatch")

        environment = str(payload.get("environment", self._environment)).lower()

        expires_ms = payload.get("expiresDate")
        expires_at = (
            datetime.datetime.fromtimestamp(int(expires_ms) / 1000, tz=datetime.UTC)
            if expires_ms is not None
            else None
        )
        revocation_ms = payload.get("revocationDate")
        revoked = revocation_ms is not None

        if "transactionId" not in payload:
            raise ValidationFailedError("StoreKit transaction missing transactionId")

        return VerifiedTransaction(
            transaction_id=str(payload["transactionId"]),
            original_transaction_id=str(
                payload.get("originalTransactionId", payload["transactionId"])
            ),
            product_id=str(payload.get("productId", "")),
            expires_at=expires_at,
            revoked=revoked,
            environment=environment,
        )


_verifier_singleton: StoreKitVerifier | None = None


def get_storekit_verifier() -> StoreKitVerifier:
    global _verifier_singleton
    if _verifier_singleton is None:
        _verifier_singleton = StoreKitVerifier()
    return _verifier_singleton
