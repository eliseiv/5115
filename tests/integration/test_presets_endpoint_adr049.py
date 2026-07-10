"""Integration: GET /v1/presets localization (ADR-049).

Extends the ADR-035 endpoint tests with locale resolution over the wire. Uses the shared hermetic
``client`` (real PG container, faked external clients, rate limits forced open). Covers:
- backward compatibility: no ``?locale=`` and no env → ``locale:"en"`` + EN texts (ADR-035 parity);
- per-instance default: ``PRESETS_DEFAULT_LOCALE=ru`` (settings overridden) → RU + ``locale:"ru"``;
- explicit query: ``?locale=ru`` → RU; ``?locale=en`` on a ru-instance → EN; ``?locale=de`` → 422;
- ``Accept-Language: ru`` → RU;
- ``id``/``icon`` stable and order preserved across locales;
- 401 without a JWT.

The per-instance default is overridden by mutating the process-wide cached Settings instance
(same approach as test_presets_endpoint_adr035's restore_provider), restored after each test.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import auth_headers, seed_user

_EXPECTED_IDS = [
    "plan_week",
    "meeting_notes",
    "tasks_from_photo",
    "design_brief",
    "daily_review",
    "summarize_text",
    "project_structure",
]
# A couple of RU ground-truth titles (ADR-049 §1.1) to prove localization end-to-end.
_RU_TITLE_PLAN_WEEK = "Планирование недели"
_EN_TITLE_PLAN_WEEK = "Plan Week"


@pytest.fixture
def restore_default_locale() -> Iterator[None]:
    """Snapshot/restore the per-instance default locale (cached Settings singleton is mutated)."""
    s = get_settings()
    orig = s.presets_default_locale
    yield
    s.presets_default_locale = orig


def _title_of(presets: list[dict[str, object]], preset_id: str) -> object:
    return next(p["title"] for p in presets if p["id"] == preset_id)


# ----------------------------- auth gate -----------------------------
@pytest.mark.asyncio
async def test_presets_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/v1/presets")
    assert r.status_code == 401


# ----------------------------- backward compatibility (EN default) -----------------------------
@pytest.mark.asyncio
async def test_default_no_params_is_english(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_default_locale: None,
) -> None:
    get_settings().presets_default_locale = "en"
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/presets", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locale"] == "en"
    assert [p["id"] for p in body["presets"]] == _EXPECTED_IDS
    assert _title_of(body["presets"], "plan_week") == _EN_TITLE_PLAN_WEEK


# ----------------------------- per-instance default = ru -----------------------------
@pytest.mark.asyncio
async def test_instance_default_ru_returns_russian(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_default_locale: None,
) -> None:
    get_settings().presets_default_locale = "ru"
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/presets", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locale"] == "ru"
    assert _title_of(body["presets"], "plan_week") == _RU_TITLE_PLAN_WEEK


# ----------------------------- explicit query overrides -----------------------------
@pytest.mark.asyncio
async def test_query_locale_ru_returns_russian(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_default_locale: None,
) -> None:
    get_settings().presets_default_locale = "en"  # instance is EN by default
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/presets?locale=ru", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locale"] == "ru"
    assert _title_of(body["presets"], "plan_week") == _RU_TITLE_PLAN_WEEK


@pytest.mark.asyncio
async def test_query_locale_en_on_ru_instance_returns_english(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_default_locale: None,
) -> None:
    get_settings().presets_default_locale = "ru"  # ru instance
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/presets?locale=en", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locale"] == "en"
    assert _title_of(body["presets"], "plan_week") == _EN_TITLE_PLAN_WEEK


@pytest.mark.asyncio
async def test_query_locale_unsupported_returns_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/presets?locale=de", headers=auth_headers(uid))
    assert r.status_code == 422, r.text


# ----------------------------- Accept-Language -----------------------------
@pytest.mark.asyncio
async def test_accept_language_ru_returns_russian(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_default_locale: None,
) -> None:
    get_settings().presets_default_locale = "en"
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get(
        "/v1/presets",
        headers={**auth_headers(uid), "Accept-Language": "ru-RU,en;q=0.8"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["locale"] == "ru"
    assert _title_of(body["presets"], "plan_week") == _RU_TITLE_PLAN_WEEK


@pytest.mark.asyncio
async def test_query_beats_accept_language(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    restore_default_locale: None,
) -> None:
    get_settings().presets_default_locale = "en"
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get(
        "/v1/presets?locale=en",
        headers={**auth_headers(uid), "Accept-Language": "ru-RU"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["locale"] == "en"


# ----------------------------- id/icon stable & order across locales -----------------------------
@pytest.mark.asyncio
async def test_id_icon_and_order_stable_across_locales(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r_en = await client.get("/v1/presets?locale=en", headers=auth_headers(uid))
    r_ru = await client.get("/v1/presets?locale=ru", headers=auth_headers(uid))
    assert r_en.status_code == 200 and r_ru.status_code == 200
    en = r_en.json()["presets"]
    ru = r_ru.json()["presets"]
    # Order + ids identical.
    assert [p["id"] for p in en] == _EXPECTED_IDS
    assert [p["id"] for p in ru] == _EXPECTED_IDS
    # icons identical per position (not localized).
    assert [p["icon"] for p in en] == [p["icon"] for p in ru]
    # titles/prompts differ (localized).
    assert _title_of(en, "plan_week") != _title_of(ru, "plan_week")


@pytest.mark.asyncio
async def test_response_has_exactly_locale_and_presets(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/presets?locale=ru", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    body = r.json()
    # StrictModel top level: exactly {locale, presets}.
    assert set(body.keys()) == {"locale", "presets"}
    for p in body["presets"]:
        assert set(p.keys()) == {"id", "title", "icon", "prompt"}
