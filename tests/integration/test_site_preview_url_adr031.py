"""Integration: site.preview URL shape — ADR-031 absolute URL on our domain.

Real PostgreSQL. Exercises SiteToolHandlers._preview directly (the server-side handler) so the
raw {url, expiresAt} payload is asserted before ADR-028 strips it from serverTools[].summary.

Covers:
  * SERVICE_DOMAIN set => absolute https URL, exact format, no double slash before /v1/.
  * SERVICE_DOMAIN empty => relative /v1/preview/... fallback (NOT localhost).
  * Invariance: expiresAt + token match build_token (absolute form does not change signature/TTL).
  * Regression: the path inside the absolute URL is served by GET /v1/preview/... (200).

The preview HMAC secret is set on the cached settings so the handler's signer and the route's
verifier share it (same pattern as test_preview_endpoint.py).
"""

from __future__ import annotations

import datetime
import re
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.config import get_settings
from app.website.service import WebsiteService
from app.website.signed_url import SignedPreview, build_token
from app.website.tools import SiteToolHandlers
from tests.conftest import seed_user

_SECRET = "adr031-secret-0123456789abcdef0123456789abcdef0123"
_DOMAIN = "broadnova.shop"


@pytest.fixture
def preview_secret() -> AsyncIterator[None]:
    settings = get_settings()
    orig = settings.preview_url_secret
    orig_ttl = settings.preview_url_ttl_seconds
    settings.preview_url_secret = _SECRET
    settings.preview_url_ttl_seconds = 900
    yield
    settings.preview_url_secret = orig
    settings.preview_url_ttl_seconds = orig_ttl


@pytest.fixture
def with_domain() -> AsyncIterator[None]:
    settings = get_settings()
    orig = settings.service_domain
    settings.service_domain = _DOMAIN
    yield
    settings.service_domain = orig


@pytest.fixture
def no_domain() -> AsyncIterator[None]:
    settings = get_settings()
    orig = settings.service_domain
    settings.service_domain = ""
    yield
    settings.service_domain = orig


def _handlers(session: AsyncSession) -> SiteToolHandlers:
    return SiteToolHandlers(session, WebsiteService(session), AuditService(session))


def _token_from_url(url: str, pid: uuid.UUID, *, entry: str = "index.html") -> str:
    """Extract the signed token segment from an absolute preview URL for exact-match asserts."""
    m = re.match(
        rf"https://[^/]+/v1/preview/{re.escape(str(pid))}/([^/]+)/{re.escape(entry)}$", url
    )
    assert m, url
    return m.group(1)


async def _seed_project_with_index(
    maker: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, str, uuid.UUID]:
    """Create a user + project (external id) + index.html. Returns (user_id, ext_id, project_id)."""
    async with maker() as s:
        uid = await seed_user(s, balance=0)
        ws = WebsiteService(s)
        project = await ws.resolve_project(user_id=uid, external_project_id="adr031-proj")
        await ws.write_file(
            project=project,
            path="index.html",
            content=b"<h1>landing</h1>",
            content_type="text/html",
        )
        await s.commit()
        return uid, "adr031-proj", project.id


@pytest.mark.asyncio
async def test_preview_absolute_url_with_service_domain(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    preview_secret: None,
    with_domain: None,
) -> None:
    uid, ext, pid = await _seed_project_with_index(db_sessionmaker)
    async with db_sessionmaker() as s:
        exec_ = await _handlers(s).execute(
            tool_name="site.preview",
            args={},  # entry defaults to index.html
            user_id=uid,
            external_project_id=ext,
            session_id=uuid.uuid4(),
        )
    assert not exec_.is_error, exec_
    assert exec_.result is not None
    url = exec_.result["url"]
    # Exact absolute format; default entry index.html.
    token = _token_from_url(url, pid)
    assert url == f"https://{_DOMAIN}/v1/preview/{pid}/{token}/index.html"
    # No double slash anywhere except right after the scheme (https://).
    assert "//" not in url.replace("https://", "", 1)


@pytest.mark.asyncio
async def test_preview_relative_fallback_without_service_domain(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    preview_secret: None,
    no_domain: None,
) -> None:
    uid, ext, pid = await _seed_project_with_index(db_sessionmaker)
    async with db_sessionmaker() as s:
        exec_ = await _handlers(s).execute(
            tool_name="site.preview",
            args={},
            user_id=uid,
            external_project_id=ext,
            session_id=uuid.uuid4(),
        )
    assert not exec_.is_error, exec_
    assert exec_.result is not None
    url = exec_.result["url"]
    # Relative fallback: no scheme, no host, NOT localhost.
    assert url.startswith(f"/v1/preview/{pid}/")
    assert url.endswith("/index.html")
    assert "http" not in url
    assert "localhost" not in url


@pytest.mark.asyncio
async def test_preview_custom_entry_in_absolute_url(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    preview_secret: None,
    with_domain: None,
) -> None:
    uid, ext, pid = await _seed_project_with_index(db_sessionmaker)
    async with db_sessionmaker() as s:
        exec_ = await _handlers(s).execute(
            tool_name="site.preview",
            args={"entry": "about.html"},
            user_id=uid,
            external_project_id=ext,
            session_id=uuid.uuid4(),
        )
    assert exec_.result is not None
    assert exec_.result["url"].endswith("/about.html")
    assert exec_.result["url"].startswith(f"https://{_DOMAIN}/v1/preview/{pid}/")


@pytest.mark.asyncio
async def test_absolute_form_does_not_change_signature_or_ttl(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    preview_secret: None,
    with_domain: None,
) -> None:
    """expiresAt + the token in the absolute URL match build_token (deterministic clock)."""
    uid, ext, pid = await _seed_project_with_index(db_sessionmaker)
    # Pin issuance time so build_token is deterministic and we can compare the token byte-for-byte.
    fixed_now = 1_700_000_000
    expected: SignedPreview = build_token(project_id=pid, owner_user_id=uid, now=fixed_now)

    def _fixed_build_token(*, project_id: uuid.UUID, owner_user_id: uuid.UUID) -> SignedPreview:
        return build_token(project_id=project_id, owner_user_id=owner_user_id, now=fixed_now)

    monkeypatch.setattr("app.website.tools.build_token", _fixed_build_token)

    async with db_sessionmaker() as s:
        exec_ = await _handlers(s).execute(
            tool_name="site.preview",
            args={},
            user_id=uid,
            external_project_id=ext,
            session_id=uuid.uuid4(),
        )

    assert exec_.result is not None
    token = _token_from_url(exec_.result["url"], pid)
    # Token in the absolute URL is exactly the build_token output (absolute form does not re-sign).
    assert token == expected.token
    # expiresAt is the ISO8601 of build_token.expires_at (TTL unchanged by the absolute form).
    expected_iso = datetime.datetime.fromtimestamp(expected.expires_at, tz=datetime.UTC).isoformat()
    assert exec_.result["expiresAt"] == expected_iso


@pytest.mark.asyncio
async def test_absolute_url_path_is_served_by_preview_route(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    preview_secret: None,
    with_domain: None,
) -> None:
    """Regression: strip the path from the absolute URL → GET /v1/preview/... serves the file (200).

    Proves the absolute URL's signed token is valid for the public preview router (the host prefix
    is cosmetic; the path + token are unchanged vs the relative form).
    """
    uid, ext, pid = await _seed_project_with_index(db_sessionmaker)
    async with db_sessionmaker() as s:
        exec_ = await _handlers(s).execute(
            tool_name="site.preview",
            args={},
            user_id=uid,
            external_project_id=ext,
            session_id=uuid.uuid4(),
        )
    assert exec_.result is not None
    url = exec_.result["url"]
    # Drop scheme+host → request path against the in-process app.
    path = re.sub(r"^https://[^/]+", "", url)
    assert path.startswith(f"/v1/preview/{pid}/")
    r = await client.get(path)
    assert r.status_code == 200, r.text
    assert r.content == b"<h1>landing</h1>"
    assert r.headers["content-type"].startswith("text/html")
