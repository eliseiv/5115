"""Integration: WebsiteService + SiteToolHandlers (ADR-010/011, website-builder/09-testing.md).

Real PostgreSQL. Covers limits, path guard, content-type allowlist, upsert, owner isolation,
delete, and server-side tool audit (tool_mutation in the same transaction).
"""

from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import AuditService
from app.config import get_settings
from app.website.service import SiteFileError, WebsiteService
from app.website.tools import SiteToolHandlers
from tests.conftest import seed_user


def _ws(session: AsyncSession) -> WebsiteService:
    return WebsiteService(session)


def _handlers(session: AsyncSession) -> SiteToolHandlers:
    return SiteToolHandlers(session, WebsiteService(session), AuditService(session))


async def _seed_session(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    """Create a chat_sessions row so tool_mutation audit (FK session_id) is satisfiable.

    In the real flow the orchestrator always has a live session; the handler unit-style tests
    must mirror that FK precondition (audit_logs.session_id → chat_sessions.id).
    """
    sid = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO chat_sessions (id, user_id, project_id, mode) "
            "VALUES (:id, :uid, 'proj', 'credits')"
        ),
        {"id": str(sid), "uid": str(user_id)},
    )
    await session.flush()
    return sid


# --------------------------- upsert + size consistency ---------------------------
@pytest.mark.asyncio
async def test_write_creates_project_and_file_then_upserts(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=0)
    ws = _ws(db_session)
    project = await ws.resolve_project(user_id=uid, external_project_id="proj-1")
    r1 = await ws.write_file(
        project=project, path="index.html", content=b"<h1>hi</h1>", content_type="text/html"
    )
    assert r1.file_count == 1
    assert r1.project_bytes == len(b"<h1>hi</h1>")

    # Re-resolve must be idempotent (same project).
    project2 = await ws.resolve_project(user_id=uid, external_project_id="proj-1")
    assert project2.id == project.id

    # Overwrite same path updates content/size, not file count.
    r2 = await ws.write_file(
        project=project,
        path="index.html",
        content=b"<h1>hello world</h1>",
        content_type="text/html",
    )
    assert r2.file_count == 1
    assert r2.project_bytes == len(b"<h1>hello world</h1>")

    file = await ws.read_file(project_id=project.id, path="index.html")
    assert file is not None
    assert file.content == b"<h1>hello world</h1>"
    assert file.size == len(b"<h1>hello world</h1>")


# --------------------------- limits ---------------------------
@pytest.mark.asyncio
async def test_file_too_large_rejected(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=0)
    ws = _ws(db_session)
    project = await ws.resolve_project(user_id=uid, external_project_id="p")
    too_big = b"x" * (get_settings().preview_max_file_bytes + 1)
    with pytest.raises(SiteFileError) as exc:
        await ws.write_file(
            project=project, path="big.html", content=too_big, content_type="text/html"
        )
    assert exc.value.code == "file_too_large"
    # nothing stored
    n = await db_session.scalar(
        text("SELECT count(*) FROM site_files WHERE project_id=:p"), {"p": str(project.id)}
    )
    assert int(n) == 0


@pytest.mark.asyncio
async def test_project_too_large_rejected(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = await seed_user(db_session, balance=0)
    settings = get_settings()
    orig = settings.preview_max_project_bytes
    settings.preview_max_project_bytes = 100
    try:
        ws = _ws(db_session)
        project = await ws.resolve_project(user_id=uid, external_project_id="p")
        await ws.write_file(
            project=project, path="a.html", content=b"x" * 60, content_type="text/html"
        )
        with pytest.raises(SiteFileError) as exc:
            await ws.write_file(
                project=project, path="b.html", content=b"x" * 60, content_type="text/html"
            )
        assert exc.value.code == "project_too_large"
    finally:
        settings.preview_max_project_bytes = orig


@pytest.mark.asyncio
async def test_too_many_files_rejected(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=0)
    settings = get_settings()
    orig = settings.preview_max_files
    settings.preview_max_files = 2
    try:
        ws = _ws(db_session)
        project = await ws.resolve_project(user_id=uid, external_project_id="p")
        await ws.write_file(project=project, path="a.html", content=b"a", content_type="text/html")
        await ws.write_file(project=project, path="b.html", content=b"b", content_type="text/html")
        with pytest.raises(SiteFileError) as exc:
            await ws.write_file(
                project=project, path="c.html", content=b"c", content_type="text/html"
            )
        assert exc.value.code == "too_many_files"
    finally:
        settings.preview_max_files = orig


# --------------------------- path guard + content-type ---------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_path", ["../escape.html", "/abs.html", "a\\b.html", "a\x00b.html"])
async def test_write_rejects_unsafe_path(db_session: AsyncSession, bad_path: str) -> None:
    uid = await seed_user(db_session, balance=0)
    ws = _ws(db_session)
    project = await ws.resolve_project(user_id=uid, external_project_id="p")
    with pytest.raises(SiteFileError) as exc:
        await ws.write_file(project=project, path=bad_path, content=b"x", content_type="text/html")
    assert exc.value.code == "invalid_path"


@pytest.mark.asyncio
async def test_write_rejects_content_type_outside_allowlist(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=0)
    ws = _ws(db_session)
    project = await ws.resolve_project(user_id=uid, external_project_id="p")
    with pytest.raises(SiteFileError) as exc:
        await ws.write_file(
            project=project, path="x.php", content=b"<?php ?>", content_type="application/x-php"
        )
    assert exc.value.code == "invalid_content_type"


# --------------------------- owner isolation (IDOR) ---------------------------
@pytest.mark.asyncio
async def test_distinct_users_get_distinct_projects_same_external_id(
    db_session: AsyncSession,
) -> None:
    a = await seed_user(db_session, balance=0)
    b = await seed_user(db_session, balance=0)
    ws = _ws(db_session)
    pa = await ws.resolve_project(user_id=a, external_project_id="shared-id")
    pb = await ws.resolve_project(user_id=b, external_project_id="shared-id")
    assert pa.id != pb.id  # same external id, different owners → different projects

    await ws.write_file(project=pa, path="index.html", content=b"A", content_type="text/html")
    # B's project does not see A's file.
    assert await ws.read_file(project_id=pb.id, path="index.html") is None
    # B cannot reach A's project via get_existing_project with its own user_id.
    assert await ws.get_existing_project(user_id=b, external_project_id="shared-id") == pb


# --------------------------- delete + stats ---------------------------
@pytest.mark.asyncio
async def test_delete_removes_file_and_updates_stats(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=0)
    ws = _ws(db_session)
    project = await ws.resolve_project(user_id=uid, external_project_id="p")
    await ws.write_file(project=project, path="a.html", content=b"aaa", content_type="text/html")
    deleted, stats = await ws.delete_file(project=project, path="a.html")
    assert deleted is True
    assert stats.file_count == 0
    assert stats.project_bytes == 0
    assert await ws.read_file(project_id=project.id, path="a.html") is None


# --------------------------- server-side tool: write_file → audit tool_mutation ---------
@pytest.mark.asyncio
async def test_site_write_file_tool_audits_mutation_in_same_session(
    db_session: AsyncSession,
) -> None:
    uid = await seed_user(db_session, balance=0)
    sid = await _seed_session(db_session, uid)
    handlers = _handlers(db_session)
    content_b64 = base64.b64encode(b"<h1>landing</h1>").decode()
    ex = await handlers.execute(
        tool_name="site.write_file",
        args={
            "path": "index.html",
            "content": content_b64,
            "contentType": "text/html",
            "encoding": "base64",
        },
        user_id=uid,
        external_project_id="proj",
        session_id=sid,
    )
    assert ex.is_error is False
    assert ex.result is not None
    assert ex.result["path"] == "index.html"
    await db_session.commit()

    muts = await db_session.scalar(
        text("SELECT count(*) FROM audit_logs WHERE user_id=:u AND event_type='tool_mutation'"),
        {"u": str(uid)},
    )
    assert int(muts) == 1
    # The file is persisted in the same transaction.
    files = await db_session.scalar(text("SELECT count(*) FROM site_files"))
    assert int(files) == 1


@pytest.mark.asyncio
async def test_site_delete_tool_audits_mutation(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=0)
    sid = await _seed_session(db_session, uid)
    handlers = _handlers(db_session)
    await handlers.execute(
        tool_name="site.write_file",
        args={
            "path": "a.html",
            "content": base64.b64encode(b"x").decode(),
            "contentType": "text/html",
            "encoding": "base64",
        },
        user_id=uid,
        external_project_id="proj",
        session_id=sid,
    )
    ex = await handlers.execute(
        tool_name="site.delete",
        args={"path": "a.html"},
        user_id=uid,
        external_project_id="proj",
        session_id=sid,
    )
    assert ex.is_error is False
    await db_session.commit()
    muts = await db_session.scalar(
        text("SELECT count(*) FROM audit_logs WHERE user_id=:u AND event_type='tool_mutation'"),
        {"u": str(uid)},
    )
    assert int(muts) == 2  # write + delete


@pytest.mark.asyncio
async def test_site_read_tool_does_not_audit_mutation(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=0)
    sid = await _seed_session(db_session, uid)
    handlers = _handlers(db_session)
    await handlers.execute(
        tool_name="site.write_file",
        args={
            "path": "a.html",
            "content": base64.b64encode(b"<p>x</p>").decode(),
            "contentType": "text/html",
            "encoding": "base64",
        },
        user_id=uid,
        external_project_id="proj",
        session_id=sid,
    )
    ex = await handlers.execute(
        tool_name="site.read",
        args={"path": "a.html"},
        user_id=uid,
        external_project_id="proj",
        session_id=sid,
    )
    assert ex.is_error is False
    assert ex.result is not None
    assert ex.result["content"] == "<p>x</p>"
    await db_session.commit()
    # Only the write produced a mutation audit, not the read.
    muts = await db_session.scalar(
        text("SELECT count(*) FROM audit_logs WHERE user_id=:u AND event_type='tool_mutation'"),
        {"u": str(uid)},
    )
    assert int(muts) == 1


@pytest.mark.asyncio
async def test_site_write_invalid_base64_is_error_no_5xx(db_session: AsyncSession) -> None:
    uid = await seed_user(db_session, balance=0)
    handlers = _handlers(db_session)
    ex = await handlers.execute(
        tool_name="site.write_file",
        args={
            "path": "a.html",
            "content": "@@@notbase64@@@",
            "contentType": "text/html",
            "encoding": "base64",
        },
        user_id=uid,
        external_project_id="proj",
        session_id=uuid.uuid4(),
    )
    assert ex.is_error is True
    assert ex.error_code == "invalid_encoding"
