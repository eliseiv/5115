"""Integration: ADR-051 — RU checkout endpoint ``POST /v1/billing/cloudpayments/checkout``.

Drives the FULL HTTP path through the real JWT auth + lazy provisioning (against the shared
testcontainers Postgres) and the checkout client, while the SINGLE outgoing broadapps call is faked
at the ``httpx`` boundary (``app.billing_cloudpayments.checkout.httpx`` is monkeypatched to a
``SimpleNamespace`` whose ``AsyncClient`` records the request and returns a scripted response). No
network to broadapps/YooKassa; the LLM is never touched (RU path). The three CLOUDPAYMENTS_API_*
configs are injected via env + ``get_settings.cache_clear()`` and restored afterwards.

Covers ADR-051: §3 outgoing multipart contract (user_id from JWT, app_id/token server-held),
§2 StrictModel (extra=forbid + productId allowlist + EmailStr), §3 upstream error mapping to a
leak-free 502, §5 config-gate 503, §1 auth 401 / rate-limit 429, §6 PII/secret log allowlist.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx as _httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import auth_headers

_URL = "/v1/billing/cloudpayments/checkout"
_APP_ID = "481d10b0-c7ee-4eeb-8618-d3a6cd7f7b9d"
_API_TOKEN = "broadapps-outgoing-bearer-secret"  # noqa: S105 - test-only static secret
_API_BASE = "https://pay.broadapps.dev/api/v1"
_TOKEN_PRODUCT = "100_tokens_9.99"
_TOKEN_PRODUCTS = f'{{"{_TOKEN_PRODUCT}": 100}}'

_OK_BODY = {
    "payment_id": "e3d7ffe4-0000-0000-0000-000000000000",
    "payment_url": "https://yoomoney.ru/checkout/payments/v2/contract?orderId=abc",
    "status": "pending",
    "expires_at": None,
}


# --------------------------- fake outgoing broadapps client ---------------------------


class _FakeResponse:
    def __init__(
        self, status_code: int, json_data: Any = None, *, json_raises: bool = False
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self._json_raises = json_raises

    def json(self) -> Any:
        if self._json_raises:
            raise ValueError("not json")
        return self._json_data


class _Broadapps:
    """Scripts + records the single faked outgoing ``POST {base}/payments/link`` call."""

    def __init__(self) -> None:
        self.calls = 0
        self.url: str | None = None
        self.files: dict[str, Any] | None = None
        self.headers: dict[str, str] | None = None
        self.init_kwargs: dict[str, Any] = {}
        self._response: _FakeResponse | None = None
        self._exc: BaseException | None = None

    def respond(
        self, status_code: int, json_data: Any = None, *, json_raises: bool = False
    ) -> None:
        self._response = _FakeResponse(status_code, json_data, json_raises=json_raises)
        self._exc = None

    def fail(self, exc: BaseException) -> None:
        self._exc = exc
        self._response = None

    async def _post(
        self,
        url: str,
        *,
        files: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        self.calls += 1
        self.url = url
        self.files = files
        self.headers = headers
        if self._exc is not None:
            raise self._exc
        assert self._response is not None, "broadapps fake not scripted"
        return self._response


def _make_fake_httpx(broadapps: _Broadapps) -> SimpleNamespace:
    """A drop-in for the ``httpx`` module name used inside checkout.py (surgical: only that ref)."""

    class _FakeAsyncClient:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            broadapps.init_kwargs = kwargs

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def post(
            self,
            url: str,
            *,
            files: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
        ) -> _FakeResponse:
            return await broadapps._post(url, files=files, headers=headers)

    return SimpleNamespace(
        AsyncClient=_FakeAsyncClient,
        TimeoutException=_httpx.TimeoutException,
        RequestError=_httpx.RequestError,
        ConnectError=_httpx.ConnectError,
    )


# ----------------------------------- fixtures -----------------------------------


@pytest.fixture
def broadapps() -> _Broadapps:
    return _Broadapps()


@pytest.fixture
async def checkout_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    broadapps: _Broadapps,
) -> AsyncIterator[AsyncClient]:
    """ASGI client with checkout configured, rate-limit allowed, and the outgoing httpx faked."""
    from app import deps
    from app.api_gateway.routers import billing_cloudpayments as cp_router
    from app.billing_cloudpayments import checkout as checkout_mod
    from app.main import create_app

    monkeypatch.setenv("CLOUDPAYMENTS_APP_ID", _APP_ID)
    monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", _API_TOKEN)
    monkeypatch.setenv("CLOUDPAYMENTS_API_BASE", _API_BASE)
    monkeypatch.setenv("TOKEN_PRODUCTS", _TOKEN_PRODUCTS)
    get_settings.cache_clear()

    # Fake ONLY the httpx reference inside checkout.py so no real socket opens to broadapps and the
    # test's own httpx.AsyncClient (ASGI transport) is untouched.
    monkeypatch.setattr(checkout_mod, "httpx", _make_fake_httpx(broadapps))

    # The router imported enforce_other_limits by name at load; patch it there. Default is allow.
    async def _allow(*, user_id: uuid.UUID) -> bool:
        return True

    monkeypatch.setattr(cp_router, "enforce_other_limits", _allow)

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


# ----------------------------------- happy path -----------------------------------


async def test_checkout_subscription_happy_path_returns_200_and_maps_fields(
    checkout_client: AsyncClient, broadapps: _Broadapps
) -> None:
    broadapps.respond(201, _OK_BODY)
    uid = uuid.uuid4()

    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "user@example.com"},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "paymentId": _OK_BODY["payment_id"],
        "paymentUrl": _OK_BODY["payment_url"],
        "status": "pending",
        "expiresAt": None,
    }


async def test_checkout_outgoing_multipart_uses_jwt_user_id_and_server_config(
    checkout_client: AsyncClient, broadapps: _Broadapps
) -> None:
    broadapps.respond(201, _OK_BODY)
    uid = uuid.uuid4()

    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "buyer@example.com"},
        headers=auth_headers(uid),
    )
    assert resp.status_code == 200, resp.text

    # Outgoing contract (ADR-051 §3): fixed host, multipart via files=, JWT-derived user_id.
    assert broadapps.calls == 1
    assert broadapps.url == f"{_API_BASE}/payments/link"
    assert broadapps.files is not None, "must send multipart via files= (not data=/urlencoded)"
    files = broadapps.files
    assert files["user_id"] == (None, str(uid))  # from JWT sub, NOT the body
    assert files["app_id"] == (None, _APP_ID)  # server-held config
    assert files["product_id"] == (None, "week_6.99_nottrial")
    assert files["customer_email"] == (None, "buyer@example.com")
    assert broadapps.headers is not None
    assert broadapps.headers["Authorization"] == f"Bearer {_API_TOKEN}"
    assert broadapps.headers["Accept"] == "application/json"
    assert broadapps.init_kwargs.get("timeout") == 15.0


async def test_checkout_accepts_upstream_200_as_success(
    checkout_client: AsyncClient, broadapps: _Broadapps
) -> None:
    # broadapps success is 201, but any 2xx is accepted defensively (ADR-051 §3).
    broadapps.respond(200, {**_OK_BODY, "status": "created", "expires_at": "2026-07-04T00:00:00Z"})

    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "user@example.com"},
        headers=auth_headers(uuid.uuid4()),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["expiresAt"] == "2026-07-04T00:00:00Z"


async def test_checkout_valid_token_package_passes_allowlist(
    checkout_client: AsyncClient, broadapps: _Broadapps
) -> None:
    broadapps.respond(201, _OK_BODY)

    resp = await checkout_client.post(
        _URL,
        json={"productId": _TOKEN_PRODUCT, "customerEmail": "user@example.com"},
        headers=auth_headers(uuid.uuid4()),
    )
    assert resp.status_code == 200, resp.text
    assert broadapps.calls == 1


# ------------------------------ userId anti-tamper (§2) ------------------------------


@pytest.mark.parametrize("extra_field", ["userId", "appId", "user_id", "app_id"])
async def test_checkout_rejects_extra_body_fields_and_makes_no_call(
    checkout_client: AsyncClient, broadapps: _Broadapps, extra_field: str
) -> None:
    # StrictModel extra=forbid: a client attempt to inject userId/appId in the body => 422 and the
    # outgoing user_id can never be client-controlled.
    resp = await checkout_client.post(
        _URL,
        json={
            "productId": "week_6.99_nottrial",
            "customerEmail": "user@example.com",
            extra_field: str(uuid.uuid4()),
        },
        headers=auth_headers(uuid.uuid4()),
    )
    assert resp.status_code == 422, resp.text
    assert broadapps.calls == 0


# ------------------------------ product validation (§2) ------------------------------


async def test_checkout_unknown_product_returns_422_without_call(
    checkout_client: AsyncClient, broadapps: _Broadapps
) -> None:
    resp = await checkout_client.post(
        _URL,
        json={"productId": "totally-not-a-product", "customerEmail": "user@example.com"},
        headers=auth_headers(uuid.uuid4()),
    )
    assert resp.status_code == 422, resp.text
    assert broadapps.calls == 0


async def test_checkout_tokens_pattern_not_in_map_returns_422(
    checkout_client: AsyncClient, broadapps: _Broadapps
) -> None:
    # Looks like a token product (NNN_tokens) but is NOT in TOKEN_PRODUCTS => credits<=0 => 422
    # (anti-tamper: we never sell what the webhook could not credit).
    resp = await checkout_client.post(
        _URL,
        json={"productId": "999_tokens_extra", "customerEmail": "user@example.com"},
        headers=auth_headers(uuid.uuid4()),
    )
    assert resp.status_code == 422, resp.text
    assert broadapps.calls == 0


# ------------------------------ customerEmail validation (§2) ------------------------------


async def test_checkout_invalid_email_returns_422(
    checkout_client: AsyncClient, broadapps: _Broadapps
) -> None:
    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "not-an-email"},
        headers=auth_headers(uuid.uuid4()),
    )
    assert resp.status_code == 422, resp.text
    assert broadapps.calls == 0


async def test_checkout_missing_email_returns_422(
    checkout_client: AsyncClient, broadapps: _Broadapps
) -> None:
    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial"},
        headers=auth_headers(uuid.uuid4()),
    )
    assert resp.status_code == 422, resp.text
    assert broadapps.calls == 0


# ------------------------- upstream errors -> leak-free 502 (§3) -------------------------


@pytest.mark.parametrize(
    "script",
    [
        ("timeout", _httpx.TimeoutException("read timed out")),
        ("connect", _httpx.ConnectError("connection refused")),
    ],
)
async def test_checkout_transport_error_maps_to_502(
    checkout_client: AsyncClient, broadapps: _Broadapps, script: tuple[str, BaseException]
) -> None:
    _name, exc = script
    broadapps.fail(exc)

    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "user@example.com"},
        headers=auth_headers(uuid.uuid4()),
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "upstream_error"


@pytest.mark.parametrize("status_code", [400, 401, 500, 503])
async def test_checkout_non_2xx_upstream_maps_to_502_without_leak(
    checkout_client: AsyncClient, broadapps: _Broadapps, status_code: int
) -> None:
    # Upstream error body carries a canary that must NEVER reach the client, along with our token.
    broadapps.respond(status_code, {"error": "UPSTREAM_CANARY_LEAK"})

    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "user@example.com"},
        headers=auth_headers(uuid.uuid4()),
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "upstream_error"
    text = resp.text
    assert "UPSTREAM_CANARY_LEAK" not in text
    assert _API_TOKEN not in text
    assert _APP_ID not in text
    assert str(status_code) not in text or status_code == 502


async def test_checkout_non_json_body_maps_to_502(
    checkout_client: AsyncClient, broadapps: _Broadapps
) -> None:
    broadapps.respond(201, json_raises=True)

    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "user@example.com"},
        headers=auth_headers(uuid.uuid4()),
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "upstream_error"


async def test_checkout_missing_payment_url_maps_to_502(
    checkout_client: AsyncClient, broadapps: _Broadapps
) -> None:
    broadapps.respond(201, {"payment_id": "x", "status": "pending"})  # no payment_url

    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "user@example.com"},
        headers=auth_headers(uuid.uuid4()),
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "upstream_error"


# ------------------------------ config-gate 503 (§5) ------------------------------


@pytest.mark.parametrize("empty_var", ["CLOUDPAYMENTS_APP_ID", "CLOUDPAYMENTS_API_TOKEN"])
async def test_checkout_not_configured_returns_503_without_call(
    checkout_client: AsyncClient,
    broadapps: _Broadapps,
    monkeypatch: pytest.MonkeyPatch,
    empty_var: str,
) -> None:
    # Blank either half of the config => 503 (feature not available here); no outgoing call.
    monkeypatch.setenv(empty_var, "")
    get_settings.cache_clear()

    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "user@example.com"},
        headers=auth_headers(uuid.uuid4()),
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "cloudpayments_checkout_not_configured"
    assert broadapps.calls == 0


# ------------------------------ auth 401 / rate-limit 429 (§1) ------------------------------


async def test_checkout_without_jwt_returns_401(
    checkout_client: AsyncClient, broadapps: _Broadapps
) -> None:
    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "user@example.com"},
    )
    assert resp.status_code == 401, resp.text
    assert broadapps.calls == 0


async def test_checkout_invalid_jwt_returns_401(
    checkout_client: AsyncClient, broadapps: _Broadapps
) -> None:
    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "user@example.com"},
        headers={"Authorization": "Bearer not.a.jwt"},
    )
    assert resp.status_code == 401, resp.text
    assert broadapps.calls == 0


async def test_checkout_rate_limited_returns_429_without_call(
    checkout_client: AsyncClient, broadapps: _Broadapps, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api_gateway.routers import billing_cloudpayments as cp_router

    async def _deny(*, user_id: uuid.UUID) -> bool:
        return False

    monkeypatch.setattr(cp_router, "enforce_other_limits", _deny)

    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "user@example.com"},
        headers=auth_headers(uuid.uuid4()),
    )
    assert resp.status_code == 429, resp.text
    assert broadapps.calls == 0


# ------------------------------ PII / secret log allowlist (§6) ------------------------------


async def test_checkout_success_log_has_only_allowlisted_fields(
    checkout_client: AsyncClient, broadapps: _Broadapps, caplog: pytest.LogCaptureFixture
) -> None:
    broadapps.respond(201, _OK_BODY)
    uid = uuid.uuid4()

    # Re-enable the checkout logger: the in-process Alembic migration (fileConfig default
    # disable_existing_loggers=True) disables loggers created before it ran — which would
    # swallow the structured record under caplog in a full-suite run (mirrors the webhook
    # outcome-log tests). Test-harness artifact only.
    logging.getLogger("app.billing_cloudpayments.checkout").disabled = False
    # Capture via the global root level (logger-scoped at_level does not catch the app's
    # structured records under caplog — mirrors the webhook outcome-log tests).
    caplog.set_level(logging.DEBUG)
    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "secret-pii@example.com"},
        headers=auth_headers(uid),
    )
    assert resp.status_code == 200, resp.text

    records = [r for r in caplog.records if r.msg == "cloudpayments_checkout_outcome"]
    assert len(records) == 1
    fields = records[0].extra_fields
    assert fields["result"] == "created"
    assert fields["userId"] == str(uid)
    assert fields["productId"] == "week_6.99_nottrial"
    assert fields["status"] == "pending"
    assert fields["paymentId"] == _OK_BODY["payment_id"]
    # Allowlist: no PII / secrets / app_id in the structured record.
    assert set(fields) <= {
        "result",
        "reason",
        "userId",
        "productId",
        "status",
        "paymentId",
        "requestId",
    }
    assert "secret-pii@example.com" not in str(fields)
    assert _API_TOKEN not in str(fields)
    assert _APP_ID not in str(fields)


async def test_checkout_error_log_has_reason_and_no_pii(
    checkout_client: AsyncClient, broadapps: _Broadapps, caplog: pytest.LogCaptureFixture
) -> None:
    broadapps.fail(_httpx.TimeoutException("read timed out"))
    uid = uuid.uuid4()

    # Re-enable the checkout logger (disabled by the in-process migration's
    # disable_existing_loggers) so caplog captures the record in a full-suite run.
    logging.getLogger("app.billing_cloudpayments.checkout").disabled = False
    # Capture via the global root level (see the success-log test above).
    caplog.set_level(logging.DEBUG)
    resp = await checkout_client.post(
        _URL,
        json={"productId": "week_6.99_nottrial", "customerEmail": "secret-pii@example.com"},
        headers=auth_headers(uid),
    )
    assert resp.status_code == 502, resp.text

    records = [r for r in caplog.records if r.msg == "cloudpayments_checkout_outcome"]
    assert len(records) == 1
    fields = records[0].extra_fields
    assert fields["result"] == "error"
    assert fields["reason"] == "timeout"
    assert fields["userId"] == str(uid)
    assert set(fields) <= {
        "result",
        "reason",
        "userId",
        "productId",
        "status",
        "paymentId",
        "requestId",
    }
    assert "secret-pii@example.com" not in str(fields)
    assert _API_TOKEN not in str(fields)
