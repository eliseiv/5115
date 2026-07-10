"""Integration: GET /v1/preview/{projectId}/{token}/{path} (ADR-010, website-builder/09-testing.md).

Real PostgreSQL via the shared `client`. PREVIEW_URL_SECRET is set on the cached settings so
the signer used here and the verifier inside the route share the same secret.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.website.signed_url import build_token
from tests.conftest import seed_user

_SECRET = "preview-secret-int-0123456789abcdef0123456789abcdef"


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


async def _seed_project_with_file(
    maker: async_sessionmaker[AsyncSession],
    *,
    owner: uuid.UUID,
    path: str,
    content: bytes,
    content_type: str,
) -> uuid.UUID:
    async with maker() as s:
        pid = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO projects (id, user_id, external_project_id) "
                "VALUES (:id, :uid, :ext)"
            ),
            {"id": str(pid), "uid": str(owner), "ext": "preview-proj"},
        )
        await s.execute(
            text(
                "INSERT INTO site_files (project_id, path, content, content_type, size) "
                "VALUES (:pid, :path, :content, :ct, :size)"
            ),
            {
                "pid": str(pid),
                "path": path,
                "content": content,
                "ct": content_type,
                "size": len(content),
            },
        )
        await s.commit()
        return pid


def _assert_sandbox_headers(resp: object) -> None:
    headers = resp.headers  # type: ignore[attr-defined]
    csp = headers.get("content-security-policy", "")
    assert "sandbox" in csp
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("x-frame-options")
    assert "no-store" in headers.get("cache-control", "")
    assert "set-cookie" not in {k.lower() for k in headers}


@pytest.mark.asyncio
async def test_valid_signed_url_serves_html_with_sandbox_headers(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    preview_secret: None,
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, balance=0)
    pid = await _seed_project_with_file(
        db_sessionmaker,
        owner=owner,
        path="index.html",
        content=b"<h1>site</h1>",
        content_type="text/html",
    )
    token = build_token(project_id=pid, owner_user_id=owner).token
    r = await client.get(f"/v1/preview/{pid}/{token}/index.html")
    assert r.status_code == 200, r.text
    assert r.content == b"<h1>site</h1>"
    assert r.headers["content-type"].startswith("text/html")
    _assert_sandbox_headers(r)


@pytest.mark.asyncio
async def test_content_type_from_db(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    preview_secret: None,
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, balance=0)
    pid = await _seed_project_with_file(
        db_sessionmaker, owner=owner, path="site.css", content=b"body{}", content_type="text/css"
    )
    token = build_token(project_id=pid, owner_user_id=owner).token
    r = await client.get(f"/v1/preview/{pid}/{token}/site.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")


@pytest.mark.asyncio
async def test_forged_token_403(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    preview_secret: None,
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, balance=0)
    pid = await _seed_project_with_file(
        db_sessionmaker, owner=owner, path="index.html", content=b"x", content_type="text/html"
    )
    r = await client.get(f"/v1/preview/{pid}/9999.deadbeef/index.html")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_expired_token_403(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    preview_secret: None,
) -> None:
    settings = get_settings()
    orig_ttl = settings.preview_url_ttl_seconds
    settings.preview_url_ttl_seconds = -10  # token issued already expired
    try:
        async with db_sessionmaker() as s:
            owner = await seed_user(s, balance=0)
        pid = await _seed_project_with_file(
            db_sessionmaker, owner=owner, path="index.html", content=b"x", content_type="text/html"
        )
        token = build_token(project_id=pid, owner_user_id=owner).token
        r = await client.get(f"/v1/preview/{pid}/{token}/index.html")
        assert r.status_code == 403
    finally:
        settings.preview_url_ttl_seconds = orig_ttl


@pytest.mark.asyncio
async def test_wrong_owner_in_signature_403(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    preview_secret: None,
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, balance=0)
        attacker = await seed_user(s, balance=0)
    pid = await _seed_project_with_file(
        db_sessionmaker, owner=owner, path="index.html", content=b"x", content_type="text/html"
    )
    # Token signed with a different owner than projects.user_id → 403.
    token = build_token(project_id=pid, owner_user_id=attacker).token
    r = await client.get(f"/v1/preview/{pid}/{token}/index.html")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_unknown_project_404(client: AsyncClient, preview_secret: None) -> None:
    pid = uuid.uuid4()
    token = build_token(project_id=pid, owner_user_id=uuid.uuid4()).token
    r = await client.get(f"/v1/preview/{pid}/{token}/index.html")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_missing_file_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    preview_secret: None,
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, balance=0)
    pid = await _seed_project_with_file(
        db_sessionmaker, owner=owner, path="index.html", content=b"x", content_type="text/html"
    )
    token = build_token(project_id=pid, owner_user_id=owner).token
    r = await client.get(f"/v1/preview/{pid}/{token}/nope.html")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_path_traversal_blocked_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    preview_secret: None,
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, balance=0)
    pid = await _seed_project_with_file(
        db_sessionmaker, owner=owner, path="index.html", content=b"x", content_type="text/html"
    )
    token = build_token(project_id=pid, owner_user_id=owner).token
    # The token is bound to the project; a traversal path inside the {path} segment must not
    # escape the project (normalize_site_path rejects it → read_file returns None → 404).
    r = await client.get(f"/v1/preview/{pid}/{token}/../../../etc/passwd")
    assert r.status_code in (403, 404)
