"""Integration: GET /v1/presets (ADR-035).

JWT-protected like GET /v1/tools and GET /v1/models. Uses the shared hermetic `client` (real PG
container, faked external clients, rate limits fail open without Redis). Covers:
- 401 without a JWT / with a broken bearer; 200 with a valid JWT;
- body: exactly the seven presets in the canonical deterministic order, all four fields non-empty,
  ids unique snake_case;
- response identity is provider-agnostic (ADR-033): identical body under LLM_PROVIDER anthropic vs
  openai (registry is a static, provider-neutral catalog);
- 429 when the per-user read limiter rejects.

The provider switch mutates the process-wide cached Settings instance (same approach as
test_models_endpoint_adr034), restored after each test.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import auth_headers, seed_user

# Canonical order & id set (ADR-035 §2) — must match app.chat.presets declaration order.
_EXPECTED_IDS = [
    "plan_week",
    "meeting_notes",
    "tasks_from_photo",
    "design_brief",
    "daily_review",
    "summarize_text",
    "project_structure",
]
_SNAKE_CASE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_FIELDS = ("id", "title", "icon", "prompt")


@pytest.fixture
def restore_provider() -> Iterator[None]:
    """Snapshot/restore the LLM provider field (the cached Settings singleton is mutated)."""
    s = get_settings()
    orig = s.llm_provider
    yield
    s.llm_provider = orig


# ----------------------------- auth gate -----------------------------
@pytest.mark.asyncio
async def test_presets_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/v1/presets")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_presets_broken_bearer_401(client: AsyncClient) -> None:
    r = await client.get("/v1/presets", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


# ----------------------------- happy path / contract -----------------------------
@pytest.mark.asyncio
async def test_presets_returns_seven_in_order(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/presets", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    presets = r.json()["presets"]
    assert len(presets) == 7
    assert [p["id"] for p in presets] == _EXPECTED_IDS


@pytest.mark.asyncio
async def test_presets_all_fields_non_empty_and_ids_unique_snake_case(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/presets", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    presets = r.json()["presets"]
    ids = [p["id"] for p in presets]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"
    for p in presets:
        # StrictModel: exactly the four contract fields, nothing extra.
        assert set(p.keys()) == set(_FIELDS), f"unexpected fields on {p}"
        for field in _FIELDS:
            assert isinstance(p[field], str) and p[field].strip(), f"{field} empty on {p['id']}"
        assert _SNAKE_CASE.match(p["id"]), f"id not snake_case: {p['id']!r}"


# ----------------------------- provider-agnostic identity (ADR-033) -----------------------------
@pytest.mark.asyncio
async def test_presets_identical_regardless_of_provider(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_provider: None,
) -> None:
    # The registry is a static, provider-neutral catalog — the body must be byte-for-byte the
    # same on an Anthropic instance and an OpenAI instance (ADR-033).
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    s = get_settings()
    s.llm_provider = "anthropic"
    r_anthropic = await client.get("/v1/presets", headers=auth_headers(uid))
    assert r_anthropic.status_code == 200, r_anthropic.text

    s.llm_provider = "openai"
    r_openai = await client.get("/v1/presets", headers=auth_headers(uid))
    assert r_openai.status_code == 200, r_openai.text

    assert r_anthropic.json() == r_openai.json()


# ----------------------------- 429 when limiter rejects -----------------------------
@pytest.mark.asyncio
async def test_presets_rate_limited_429(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The presets router imported enforce_other_limits by name at module load; patch it there.
    from app.api_gateway.routers import presets as presets_router

    async def _reject(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(presets_router, "enforce_other_limits", _reject)
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/presets", headers=auth_headers(uid))
    assert r.status_code == 429, r.text
