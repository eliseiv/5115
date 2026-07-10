"""Opaque keyset-pagination cursor for the chats list (chats/02-api-contracts.md).

Encodes the ordering tuple (is_pinned, updated_at, id) of the last returned row. The list is
ordered ``is_pinned DESC, updated_at DESC, id DESC`` (BR-CH-3 + a stable id tie-break), so a
cursor lets the next page resume deterministically even when ``updated_at`` ties.
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
class ChatCursor:
    is_pinned: bool
    updated_at: datetime.datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = f"{int(self.is_pinned)}|{self.updated_at.isoformat()}|{self.id}"
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def decode(value: str) -> ChatCursor:
        try:
            raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
            pinned_str, updated_str, id_str = raw.split("|", 2)
            updated = datetime.datetime.fromisoformat(updated_str)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=datetime.UTC)
            return ChatCursor(
                is_pinned=bool(int(pinned_str)),
                updated_at=updated,
                id=uuid.UUID(id_str),
            )
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise InvalidCursorError("invalid cursor") from exc
