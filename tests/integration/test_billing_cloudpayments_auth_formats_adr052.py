"""Integration: ADR-054 — the PUBLIC CloudPayments webhook never 401s on ANY Authorization shape.

Supersedes the ADR-052 blocking-auth full-stack suite. ADR-054 removed the 401: the endpoint is
public and ``require_cloudpayments_webhook`` is observational (never raises). This regression drives
the real route ``POST /v1/billing/cloudpayments/webhook`` with every Authorization shape (none, raw,
``Bearer``, lower-case ``bearer``, ``Token``, ``Basic``, wrong token, garbage) and asserts NONE is
rejected — each reaches the handler and collapses to ``200 {"code":0}``. Hermetic: shared
testcontainers Postgres (seeded), CLOUDPAYMENTS_API_TOKEN injected via env, the broadapps verify
client FAKED (no network); no LLM.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import seed_user

_URL = "/v1/billing/cloudpayments/webhook"
_UID_UPPER = "B284721F-C3E0-4446-B00F-3C6A21F32535"
_TOKEN_CODE = "100_tokens_9.99"
_LEGACY = "cloudpayments-webhook-secret-value"  # noqa: S105 - optional legacy token (log-only now)


class FakeVerifyClient:
    def __init__(self, payments: list[dict[str, Any]]) -> None:
        self._payments = payments

    async def list_payments(self, *, device_id: uuid.UUID) -> list[dict[str, Any]]:
        return [dict(p) for p in self._payments]


def _payment() -> dict[str, Any]:
    return {
        "payment_id": "pay-fmt",
        "status": "succeeded",
        "paid_at": (
            datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(minutes=5)
        ).isoformat(),
        "product": {"code": _TOKEN_CODE, "payment_type": "one_time"},
    }


@pytest.fixture
async def cp_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    from app import deps
    from app.main import create_app

    monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", "verify-token-secret")
    monkeypatch.setenv("CLOUDPAYMENTS_WEBHOOK_TOKEN", _LEGACY)  # optional legacy: log-only, no gate
    monkeypatch.setenv("TOKEN_PRODUCTS", json.dumps({_TOKEN_CODE: 100}))
    get_settings.cache_clear()

    monkeypatch.setattr(
        deps, "get_cloudpayments_verify_client", lambda: FakeVerifyClient([_payment()])
    )

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    get_settings.cache_clear()


def _body() -> bytes:
    body = {
        "Status": "Completed",
        "OperationType": "Payment",
        "Amount": 4990,
        "Currency": "RUB",
        "AccountId": _UID_UPPER,
    }
    return json.dumps(body).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        None,  # broadapps reality: no Authorization at all
        _LEGACY,  # raw legacy token (matched=True, log-only)
        f"Bearer {_LEGACY}",
        f"bearer {_LEGACY}",  # lower-case scheme word
        f"Token {_LEGACY}",
        f"Basic {_LEGACY}",  # unrecognised scheme
        f"Bearer {_LEGACY}-wrong",  # wrong token
        "garbage",  # arbitrary raw
    ],
)
async def test_every_authorization_shape_reaches_handler_not_401(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], header: str | None
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    headers = {"Authorization": header} if header is not None else {}
    r = await cp_client.post(_URL, content=_body(), headers=headers)
    # Public endpoint: never 401/403 — the callback reaches the verification service and acks.
    assert r.status_code == 200, r.text
    assert r.json() == {"code": 0}
