"""Opaque keyset-pagination cursor for the images list (GET /v1/images).

Mirrors the chats cursor (``app/chats/cursor.py``): encodes the ordering tuple
``(created_at, id)`` of the last returned row. The list is ordered ``created_at DESC, id DESC``
(a stable id tie-break), so a cursor lets the next page resume deterministically even when
``created_at`` ties.
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
class ImageCursor:
    created_at: datetime.datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.id}"
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def decode(value: str) -> ImageCursor:
        try:
            raw = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
            created_str, id_str = raw.split("|", 1)
            created = datetime.datetime.fromisoformat(created_str)
            if created.tzinfo is None:
                created = created.replace(tzinfo=datetime.UTC)
            return ImageCursor(created_at=created, id=uuid.UUID(id_str))
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise InvalidCursorError("invalid cursor") from exc
