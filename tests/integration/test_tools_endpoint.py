"""Integration: GET /v1/tools (ADR-019, chat-orchestrator/02).

JWT-protected like all /v1/* reads. Uses the shared hermetic `client` (real PG container, faked
external clients, rate limits forced open). Verifies the auth gate (401 without token) and the
response contract (8 tools — ADR-063 removed the 8 client-side files.*/calendar.*/reminders.*
tools, leaving 5 server-side site.* + time.now/quiz.generate/image.generate, all server-side;
dotted domain names; mutating/execution flags; inputSchema present). The catalog lists
quiz.generate and image.generate unconditionally; their dialog / key gates constrain only the
LLM offer-set, not this endpoint.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user

_EXPECTED_NAMES = {
    "site.write_file",
    "site.preview",
    "site.list",
    "site.read",
    "site.delete",
    "time.now",
    "quiz.generate",
    "image.generate",
}
_MUTATING = {
    "site.write_file",
    "site.delete",
    # ADR-058 §1/§4: image.generate writes bytes + is tariffed → mutating.
    "image.generate",
}


@pytest.mark.asyncio
async def test_tools_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/v1/tools")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_tools_broken_bearer_401(client: AsyncClient) -> None:
    r = await client.get("/v1/tools", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_tools_returns_full_catalog_with_token(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/tools", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    tools = r.json()["tools"]
    assert len(tools) == 8
    assert {t["name"] for t in tools} == _EXPECTED_NAMES
    # ADR-063: no removed client-side tool is advertised anymore.
    assert not any(t["name"].startswith(("files.", "calendar.", "reminders.")) for t in tools)
    # ADR-057: quiz.generate is advertised as a server-side, non-mutating tool.
    quiz = next(t for t in tools if t["name"] == "quiz.generate")
    assert quiz["execution"] == "server"
    assert quiz["mutating"] is False
    # ADR-058: image.generate is advertised as a server-side, MUTATING tool (key-gate is offer-set
    # only; the catalog lists it unconditionally).
    image = next(t for t in tools if t["name"] == "image.generate")
    assert image["execution"] == "server"
    assert image["mutating"] is True


@pytest.mark.asyncio
async def test_tools_descriptor_contract(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/tools", headers=auth_headers(uid))
    assert r.status_code == 200
    by_name = {t["name"]: t for t in r.json()["tools"]}
    for name, tool in by_name.items():
        # Domain dotted name, never the anthropic underscore wire form (BUG-3).
        assert "." in name and "_" not in name.split(".")[0]
        assert set(tool.keys()) == {"name", "description", "mutating", "execution", "inputSchema"}
        assert tool["mutating"] is (name in _MUTATING), name
        # ADR-026/ADR-057/ADR-058: server-side == site.* (project-scoped) OR global
        # (time.now/quiz.generate/image.generate); else client.
        expected_exec = (
            "server"
            if name.startswith("site.") or name in ("time.now", "quiz.generate", "image.generate")
            else "client"
        )
        assert tool["execution"] == expected_exec, (name, tool["execution"])
        assert isinstance(tool["inputSchema"], dict) and tool["inputSchema"].get("type") == "object"
        assert tool["description"]


@pytest.mark.asyncio
async def test_tools_user_mismatch_in_token_still_serves_own_catalog(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # The catalog is identical for every authenticated user (no per-user data); a freshly-minted
    # token for an unprovisioned subject still gets a 200 (lazy provisioning, ADR-007).
    r = await client.get("/v1/tools", headers=auth_headers(uuid.uuid4()))
    assert r.status_code == 200
    assert len(r.json()["tools"]) == 8
