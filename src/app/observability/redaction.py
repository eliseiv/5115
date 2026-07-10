"""Secret redaction for logs and audit payloads (05-security.md).

Denylist of substrings in keys: *key*, *token*, *secret*, plus explicit fields like
Authorization, apiKey, transaction (StoreKit payload). Never log BYOK keys, JWTs,
StoreKit payloads. Used by both the logging formatter and the audit redactor.
"""

from __future__ import annotations

from typing import Any

REDACTED = "***REDACTED***"

# Substrings (lowercased) that mark a value as sensitive.
_DENY_SUBSTRINGS = ("key", "token", "secret", "password", "authorization", "credential")

# Explicit field names (lowercased) that carry raw secrets/payloads.
# x-admin-token is also matched by the "token" substring below; listed here explicitly so
# the admin secret is unambiguously redacted from logs/audit (ADR-009 §6).
_DENY_EXACT = (
    "apikey",
    "transaction",
    "jws",
    "receipt",
    "dek",
    "nonce",
    "encrypted_key",
    "x-admin-token",
    "x_admin_token",
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    # Status fields (e.g. keyStatus = valid|invalid|missing) are non-sensitive metadata and
    # must survive redaction (AC-7 audit byok_change); the raw BYOK key is never in such a field.
    if lowered.endswith("status"):
        return False
    if lowered in _DENY_EXACT:
        return True
    return any(sub in lowered for sub in _DENY_SUBSTRINGS)


def _redact_attachments(value: Any) -> Any:
    """Redact the base64 `data` of each inline attachment (ADR-020, 05-security.md).

    Attachment content (`attachments[].data` and decoded bytes/text) is never logged. The key is
    `data` (not matched by the generic *key*/*token*/*secret* denylist), so attachment lists get
    this targeted rule: each item's `data` becomes REDACTED while metadata (type/mediaType/
    filename) survives for diagnostics. Non-list/non-dict items are passed through redact().
    """
    if not isinstance(value, list | tuple):
        return redact(value)
    redacted_items: list[Any] = []
    for item in value:
        if isinstance(item, dict):
            redacted_item = {k: (REDACTED if k == "data" else redact(v)) for k, v in item.items()}
            redacted_items.append(redacted_item)
        else:
            redacted_items.append(redact(item))
    return redacted_items


def redact(value: Any) -> Any:
    """Recursively redact sensitive values in dicts/lists. Returns a redacted copy."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _is_sensitive_key(k):
                result[k] = REDACTED
            elif isinstance(k, str) and k.lower() == "attachments":
                result[k] = _redact_attachments(v)
            else:
                result[k] = redact(v)
        return result
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


def assert_no_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """Redaction guard for audit payloads. Returns a redacted copy (defensive)."""
    return redact(payload)  # type: ignore[no-any-return]
