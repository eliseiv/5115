"""Profile service: get/update over users (display_name) + derived accountId (profile/03)."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.models import User
from app.profile.account_id import account_id


@dataclass(frozen=True)
class ProfileView:
    account_id: str
    display_name: str | None
    created_at: datetime.datetime


def _to_view(user: User) -> ProfileView:
    return ProfileView(
        account_id=account_id(user.id),
        display_name=user.display_name,
        created_at=user.created_at,
    )


class ProfileService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> ProfileView:
        # The users row exists by lazy provisioning (ADR-007); defensive 404 otherwise.
        user = await self._session.get(User, user_id)
        if user is None:  # pragma: no cover - provisioning guarantees the row exists
            raise NotFoundError("user not found")
        return _to_view(user)

    async def update_display_name(
        self, user_id: uuid.UUID, display_name: str | None
    ) -> ProfileView:
        user = await self._session.get(User, user_id)
        if user is None:  # pragma: no cover - provisioning guarantees the row exists
            raise NotFoundError("user not found")
        user.display_name = display_name
        await self._session.flush()
        await self._session.commit()
        return _to_view(user)
