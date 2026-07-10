"""Opaque keyset-pagination cursor for the workspaces list (workspaces/02-api-contracts.md).

Encodes the ordering tuple (updated_at, id) of the last returned row. The list is ordered
``updated_at DESC, id DESC`` (ADR-036 §8 + a stable id tie-break), so a cursor lets the next page
resume deterministically even when ``updated_at`` ties. Same pattern as the chats cursor.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import uuid
from dataclasses import dataclass


class InvalidCursorError(ValueError):
    """Raised when an opaque cursor cannot be decoded → mapped to 422 at the API layer."""


@dataclass(frozen=True)
class WorkspaceCursor:
    updated_at: datetime.datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = f"{self.updated_at.isoformat()}|{self.id}"
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def decode(value: str) -> WorkspaceCursor:
        try:
            raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
            updated_str, id_str = raw.split("|", 1)
            updated = datetime.datetime.fromisoformat(updated_str)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=datetime.UTC)
            return WorkspaceCursor(updated_at=updated, id=uuid.UUID(id_str))
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise InvalidCursorError("invalid cursor") from exc
