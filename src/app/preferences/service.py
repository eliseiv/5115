"""Preferences service: lazy get + upsert patch over user_preferences (preferences/03).

GET without a row returns in-memory defaults (no DB write). PATCH upserts and only updates
the provided fields (COALESCE semantics at the use-case layer). assistant_mode is the
assistant type (chat|code) and is orthogonal to billing_mode (ADR-012).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserPreferences

DEFAULT_ASSISTANT_MODE = "chat"
# ADR-055 §3: default dialog mode when no row exists / not set.
DEFAULT_DIALOG_MODE = "smart"


@dataclass(frozen=True)
class PreferencesView:
    default_assistant_mode: str
    default_dialog_mode: str
    notifications_enabled: bool
    code_defaults: dict[str, Any]


def _defaults() -> PreferencesView:
    return PreferencesView(
        default_assistant_mode=DEFAULT_ASSISTANT_MODE,
        default_dialog_mode=DEFAULT_DIALOG_MODE,
        notifications_enabled=False,
        code_defaults={},
    )


def _to_view(row: UserPreferences) -> PreferencesView:
    return PreferencesView(
        default_assistant_mode=row.default_assistant_mode,
        default_dialog_mode=row.default_dialog_mode,
        notifications_enabled=row.notifications_enabled,
        code_defaults=dict(row.code_defaults),
    )


class PreferencesService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _load(self, user_id: uuid.UUID) -> UserPreferences | None:
        row: UserPreferences | None = await self._session.scalar(
            select(UserPreferences).where(UserPreferences.user_id == user_id)
        )
        return row

    async def get(self, user_id: uuid.UUID) -> PreferencesView:
        row = await self._load(user_id)
        if row is None:
            return _defaults()
        return _to_view(row)

    async def patch(
        self,
        user_id: uuid.UUID,
        *,
        default_assistant_mode: str | None,
        default_dialog_mode: str | None,
        notifications_enabled: bool | None,
        code_defaults: dict[str, Any] | None,
    ) -> PreferencesView:
        """Upsert preferences, updating only the provided (non-None) fields."""
        row = await self._load(user_id)
        if row is None:
            defaults = _defaults()
            row = UserPreferences(
                user_id=user_id,
                default_assistant_mode=(
                    default_assistant_mode
                    if default_assistant_mode is not None
                    else defaults.default_assistant_mode
                ),
                default_dialog_mode=(
                    default_dialog_mode
                    if default_dialog_mode is not None
                    else defaults.default_dialog_mode
                ),
                notifications_enabled=(
                    notifications_enabled
                    if notifications_enabled is not None
                    else defaults.notifications_enabled
                ),
                code_defaults=(
                    code_defaults if code_defaults is not None else defaults.code_defaults
                ),
            )
            self._session.add(row)
        else:
            if default_assistant_mode is not None:
                row.default_assistant_mode = default_assistant_mode
            if default_dialog_mode is not None:
                row.default_dialog_mode = default_dialog_mode
            if notifications_enabled is not None:
                row.notifications_enabled = notifications_enabled
            if code_defaults is not None:
                row.code_defaults = code_defaults
        await self._session.flush()
        await self._session.commit()
        return _to_view(row)

    async def get_default_assistant_mode(self, user_id: uuid.UUID) -> str:
        """Orchestrator fallback: preferences default, or 'chat' if no row (ADR-012)."""
        row = await self._load(user_id)
        if row is None:
            return DEFAULT_ASSISTANT_MODE
        return row.default_assistant_mode

    async def get_default_dialog_mode(self, user_id: uuid.UUID) -> str:
        """Orchestrator fallback: preferences default, or 'smart' if no row (ADR-055 §3)."""
        row = await self._load(user_id)
        if row is None:
            return DEFAULT_DIALOG_MODE
        return row.default_dialog_mode
