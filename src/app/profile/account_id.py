"""Deterministic human-readable accountId derivation (BR-PR-1, profile/03).

``account_id(user_id)`` is a PURE function: the same user_id always yields the same id.
Format: ``XXXX-XXXX-XXXXX`` — two 4-digit numeric groups + a 5-char alphanumeric group
(e.g. ``8472-1936-AXQ5``). It is a display mapping only, NOT a secret nor an authorization
key (authorization is always the JWT ``sub``). Not stored in the DB → no DB/compute drift.
"""

from __future__ import annotations

import hashlib
import uuid

# Unambiguous alphabet for the trailing group (no I/O/0/1 confusion).
_ALPHANUM = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def account_id(user_id: uuid.UUID) -> str:
    """Map a user UUID to a stable display id ``XXXX-XXXX-XXXXX``."""
    digest = hashlib.sha256(str(user_id).encode("utf-8")).digest()
    # First two numeric groups: 4 decimal digits each from disjoint digest slices.
    g1 = int.from_bytes(digest[0:4], "big") % 10000
    g2 = int.from_bytes(digest[4:8], "big") % 10000
    # Trailing 5-char alphanumeric group from a further disjoint slice.
    value = int.from_bytes(digest[8:16], "big")
    chars = []
    for _ in range(5):
        value, rem = divmod(value, len(_ALPHANUM))
        chars.append(_ALPHANUM[rem])
    return f"{g1:04d}-{g2:04d}-{''.join(chars)}"
