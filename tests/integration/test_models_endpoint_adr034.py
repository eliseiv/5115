"""Integration: GET /v1/models (ADR-034 §2).

JWT-protected like GET /v1/tools. Uses the shared hermetic `client` (real PG container, faked
external clients, rate limits fail open without Redis). Covers:
- 401 without a JWT / with a broken bearer;
- with a JWT: the active provider's allowlist, EXACTLY one default:true, default FIRST;
- empty allowlist → exactly one element (the instance default, default:true) — backward compat;
- non-empty allowlist WITHOUT the default → default prepended first; WITH it → order preserved;
- 429 when the per-user read limiter rejects.

The allowlist is configured by mutating the process-wide cached Settings instance (same approach as
test_adr022_project_gating mutating settings.preview_url_secret), restored after each test.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import auth_headers, seed_user


@pytest.fixture
def restore_model_settings() -> Iterator[None]:
    """Snapshot/restore the model-allowlist Settings fields (the cached singleton is mutated)."""
    s = get_settings()
    orig = (s.llm_provider, s.anthropic_models_raw, s.openai_models_raw, s.anthropic_model)
    yield
    (s.llm_provider, s.anthropic_models_raw, s.openai_models_raw, s.anthropic_model) = orig


def _set_allowlist(*, provider: str, anthropic_raw: str, anthropic_model: str) -> None:
    s = get_settings()
    s.llm_provider = provider
    s.anthropic_models_raw = anthropic_raw
    s.anthropic_model = anthropic_model


# ----------------------------- auth gate -----------------------------
@pytest.mark.asyncio
async def test_models_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/v1/models")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_models_broken_bearer_401(client: AsyncClient) -> None:
    r = await client.get("/v1/models", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


# ----------------------------- empty allowlist (backward compat) -----------------------------
@pytest.mark.asyncio
async def test_models_empty_allowlist_single_default(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_model_settings: None,
) -> None:
    _set_allowlist(provider="anthropic", anthropic_raw="{}", anthropic_model="claude-sonnet-4-5")
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    assert models == [
        {"id": "claude-sonnet-4-5", "displayName": "claude-sonnet-4-5", "default": True}
    ]


# ----------------------------- non-empty WITHOUT default → default prepended -----------------
@pytest.mark.asyncio
async def test_models_allowlist_without_default_prepends_default_first(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_model_settings: None,
) -> None:
    raw = json.dumps({"claude-haiku": "Claude Haiku", "claude-opus": "Claude Opus"})
    _set_allowlist(provider="anthropic", anthropic_raw=raw, anthropic_model="claude-sonnet-4-5")
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    # default first (displayName = id), then allowlist insertion order.
    assert [m["id"] for m in models] == ["claude-sonnet-4-5", "claude-haiku", "claude-opus"]
    # exactly one default:true and it is the first element.
    assert [m["default"] for m in models] == [True, False, False]
    assert models[0]["displayName"] == "claude-sonnet-4-5"
    assert sum(1 for m in models if m["default"]) == 1


# ----------------------------- non-empty WITH default → order preserved -----------------------
@pytest.mark.asyncio
async def test_models_allowlist_with_default_keeps_display_and_order(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_model_settings: None,
) -> None:
    raw = json.dumps({"claude-sonnet-4-5": "Claude Sonnet 4.5", "claude-haiku": "Claude Haiku"})
    _set_allowlist(provider="anthropic", anthropic_raw=raw, anthropic_model="claude-sonnet-4-5")
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    assert [m["id"] for m in models] == ["claude-sonnet-4-5", "claude-haiku"]
    assert [m["default"] for m in models] == [True, False]
    # displayName from the allowlist is kept for the default (not overwritten with the id).
    assert models[0]["displayName"] == "Claude Sonnet 4.5"
    assert sum(1 for m in models if m["default"]) == 1


# ----------------------------- exactly one default + default first invariant ------------------
@pytest.mark.asyncio
async def test_models_response_invariants(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_model_settings: None,
) -> None:
    raw = json.dumps({"a": "A", "claude-def": "Default", "b": "B"})
    _set_allowlist(provider="anthropic", anthropic_raw=raw, anthropic_model="claude-def")
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    models = r.json()["models"]
    # default present, exactly one default:true, and it is first.
    defaults = [m for m in models if m["default"]]
    assert len(defaults) == 1
    assert models[0]["default"] is True
    assert models[0]["id"] == "claude-def"
    # no duplicate ids.
    ids = [m["id"] for m in models]
    assert len(ids) == len(set(ids))


# ----------------------------- 429 when limiter rejects -----------------------------
@pytest.mark.asyncio
async def test_models_rate_limited_429(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The models router imported enforce_other_limits by name at module load; patch it there.
    from app.api_gateway.routers import models as models_router

    async def _reject(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(models_router, "enforce_other_limits", _reject)
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 429, r.text
