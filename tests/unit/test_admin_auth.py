"""Unit: require_admin / X-Admin-Token authorization (ADR-009, admin/09-testing.md).

Pure auth logic over the cached Settings (no DB). Constant-time compare against
ADMIN_API_SECRET and ADMIN_API_SECRET_PREV (rotation). Empty secrets never match.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.api_gateway.auth import _admin_token_matches, require_admin
from app.config import get_settings
from app.errors import UnauthorizedError

_SECRET = "admin-secret-current-0123456789abcdef0123456789ab"
_PREV = "admin-secret-previous-0123456789abcdef0123456789"


@pytest.fixture
def admin_secrets() -> Iterator[None]:
    settings = get_settings()
    orig, orig_prev = settings.admin_api_secret, settings.admin_api_secret_prev
    settings.admin_api_secret = _SECRET
    settings.admin_api_secret_prev = _PREV
    yield
    settings.admin_api_secret = orig
    settings.admin_api_secret_prev = orig_prev


def test_current_secret_matches(admin_secrets: None) -> None:
    assert _admin_token_matches(_SECRET) is True


def test_prev_secret_matches_during_rotation(admin_secrets: None) -> None:
    assert _admin_token_matches(_PREV) is True


def test_wrong_token_does_not_match(admin_secrets: None) -> None:
    assert _admin_token_matches("not-the-secret") is False


def test_empty_presented_does_not_match(admin_secrets: None) -> None:
    assert _admin_token_matches("") is False


def test_blank_configured_secret_never_matches() -> None:
    settings = get_settings()
    orig, orig_prev = settings.admin_api_secret, settings.admin_api_secret_prev
    settings.admin_api_secret = ""
    settings.admin_api_secret_prev = ""
    try:
        # An empty presented token must NOT authenticate against unset secrets.
        assert _admin_token_matches("") is False
        assert _admin_token_matches("anything") is False
    finally:
        settings.admin_api_secret = orig
        settings.admin_api_secret_prev = orig_prev


@pytest.mark.asyncio
async def test_require_admin_passes_with_valid_token(admin_secrets: None) -> None:
    # No exception → authorized.
    await require_admin(x_admin_token=_SECRET)


@pytest.mark.asyncio
async def test_require_admin_missing_token_401(admin_secrets: None) -> None:
    with pytest.raises(UnauthorizedError):
        await require_admin(x_admin_token=None)


@pytest.mark.asyncio
async def test_require_admin_wrong_token_401(admin_secrets: None) -> None:
    with pytest.raises(UnauthorizedError):
        await require_admin(x_admin_token="wrong")
