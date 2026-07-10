"""Unit: ADR-054 — broadapps payment verify client + PURE reconciliation (no network, no DB).

Two seams, both hermetic:
- ``CloudPaymentsVerifyClient.list_payments`` — the outgoing ``GET /users/{deviceId}/payments`` is
  driven WITHOUT the network by swapping ``httpx.AsyncClient`` for a fake that returns a scripted
  ``httpx.Response`` (or raises a scripted ``httpx`` error). Asserts the ADR-054 §2/§5 mapping:
  2xx+data -> list; 404 -> ``[]`` (permanent, NOT a 500-retry); timeout/connect/5xx/non-JSON/no-
  ``data`` -> :class:`CloudPaymentsVerificationUnavailableError` (500, retriable). The token is
  never logged/leaked.
- :func:`select_creditable_payments` / :func:`payment_statuses` / ``_parse_paid_at`` — PURE
  reconciliation (ADR-054 §6): status-in-paid-set AND fresh AND carries payment_id/product.code/
  payment_type; ``now``-referenced freshness window; robust to malformed items.

No LLM (RU path never touches one) so placeholder provider keys are irrelevant.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import httpx
import pytest

from app.billing_cloudpayments import verify
from app.billing_cloudpayments.verify import (
    CloudPaymentsVerifyClient,
    CreditablePayment,
    payment_statuses,
    select_creditable_payments,
)
from app.config import Settings
from app.errors import CloudPaymentsVerificationUnavailableError

_DEVICE = uuid.UUID("55cbe083-fcbd-4460-af62-06f9a7bea97c")
_PAID = frozenset({"succeeded"})


def _now() -> datetime.datetime:
    return datetime.datetime(2026, 7, 3, 12, 0, tzinfo=datetime.UTC)


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


def _item(
    *,
    payment_id: str = "pay-1",
    status: str = "succeeded",
    paid_at: str | None = None,
    code: str = "100_tokens_9.99",
    payment_type: str = "one_time",
    **extra: Any,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "payment_id": payment_id,
        "status": status,
        "paid_at": paid_at if paid_at is not None else _iso(_now() - datetime.timedelta(minutes=5)),
        "product": {"code": code, "payment_type": payment_type},
    }
    item.update(extra)
    return item


# ============================ select_creditable_payments (pure) ============================


def test_select_keeps_a_fresh_succeeded_payment() -> None:
    out = select_creditable_payments([_item()], paid_statuses=_PAID, now=_now(), freshness_hours=72)
    assert len(out) == 1
    p = out[0]
    assert isinstance(p, CreditablePayment)
    assert (p.payment_id, p.product_code, p.payment_type, p.status) == (
        "pay-1",
        "100_tokens_9.99",
        "one_time",
        "succeeded",
    )


@pytest.mark.parametrize("status", ["pending", "failed", "refunded", "SUCCEEDED-typo"])
def test_select_drops_non_paid_status(status: str) -> None:
    assert (
        select_creditable_payments(
            [_item(status=status)], paid_statuses=_PAID, now=_now(), freshness_hours=72
        )
        == []
    )


def test_select_status_compare_is_case_insensitive() -> None:
    # broadapps status compared as .strip().lower() against the paid set.
    out = select_creditable_payments(
        [_item(status="  Succeeded ")], paid_statuses=_PAID, now=_now(), freshness_hours=72
    )
    assert len(out) == 1 and out[0].status == "succeeded"


def test_select_honours_configured_paid_statuses() -> None:
    out = select_creditable_payments(
        [_item(status="paid")],
        paid_statuses=frozenset({"succeeded", "paid"}),
        now=_now(),
        freshness_hours=72,
    )
    assert len(out) == 1


def test_select_drops_stale_payment_outside_window() -> None:
    stale = _item(paid_at=_iso(_now() - datetime.timedelta(hours=100)))
    assert (
        select_creditable_payments([stale], paid_statuses=_PAID, now=_now(), freshness_hours=72)
        == []
    )


def test_select_keeps_payment_at_window_edge() -> None:
    edge = _item(paid_at=_iso(_now() - datetime.timedelta(hours=72)))
    out = select_creditable_payments([edge], paid_statuses=_PAID, now=_now(), freshness_hours=72)
    assert len(out) == 1  # cutoff is inclusive (>= now - window)


def test_select_tolerates_trailing_z_and_naive_paid_at() -> None:
    z = _item(paid_at=_now().replace(tzinfo=None).isoformat() + "Z")
    naive = _item(payment_id="pay-2", paid_at=_now().replace(tzinfo=None).isoformat())
    out = select_creditable_payments(
        [z, naive], paid_statuses=_PAID, now=_now(), freshness_hours=72
    )
    assert {p.payment_id for p in out} == {"pay-1", "pay-2"}


@pytest.mark.parametrize(
    "bad",
    [
        {
            "status": "succeeded",
            "paid_at": "not-a-date",
            "product": {"code": "x", "payment_type": "one_time"},
            "payment_id": "p",
        },
        {
            "status": "succeeded",
            "product": {"code": "x", "payment_type": "one_time"},
            "payment_id": "p",
        },  # no paid_at
        {
            "status": "succeeded",
            "paid_at": None,
            "product": {"code": "x", "payment_type": "one_time"},
            "payment_id": "p",
        },
    ],
)
def test_select_drops_unparseable_or_missing_paid_at(bad: dict[str, Any]) -> None:
    assert (
        select_creditable_payments([bad], paid_statuses=_PAID, now=_now(), freshness_hours=72) == []
    )


def test_select_drops_missing_payment_id() -> None:
    no_id = _item()
    del no_id["payment_id"]
    blank_id = _item(payment_id="   ")
    assert (
        select_creditable_payments(
            [no_id, blank_id], paid_statuses=_PAID, now=_now(), freshness_hours=72
        )
        == []
    )


def test_select_drops_missing_or_blank_product_code_or_type() -> None:
    no_code = _item(payment_id="a")
    no_code["product"] = {"payment_type": "one_time"}
    blank_code = _item(payment_id="b", code="   ")
    no_type = _item(payment_id="c")
    no_type["product"] = {"code": "x"}
    no_product = _item(payment_id="d")
    no_product["product"] = "not-a-dict"
    out = select_creditable_payments(
        [no_code, blank_code, no_type, no_product],
        paid_statuses=_PAID,
        now=_now(),
        freshness_hours=72,
    )
    assert out == []


def test_select_normalises_and_trims_fields() -> None:
    out = select_creditable_payments(
        [_item(payment_id="  pay-x ", code=" week_6.99 ", payment_type=" Subscription ")],
        paid_statuses=_PAID,
        now=_now(),
        freshness_hours=72,
    )
    assert len(out) == 1
    p = out[0]
    assert p.payment_id == "pay-x"
    assert p.product_code == "week_6.99"
    assert p.payment_type == "subscription"  # lower-cased


def test_select_skips_non_dict_items() -> None:
    out = select_creditable_payments(
        ["nope", 123, None, _item()],  # type: ignore[list-item]
        paid_statuses=_PAID,
        now=_now(),
        freshness_hours=72,
    )
    assert len(out) == 1


def test_payment_statuses_lists_raw_statuses_safely() -> None:
    data = [_item(status="succeeded"), _item(payment_id="p2", status="failed"), "junk"]  # type: ignore[list-item]
    assert payment_statuses(data) == ["succeeded", "failed"]


# ============================ CloudPaymentsVerifyClient.list_payments (faked httpx) ============


class _FakeResp:
    def __init__(
        self, status_code: int, *, json_body: Any = None, raw: bytes | None = None
    ) -> None:
        self.status_code = status_code
        self._json = json_body
        self._raw = raw

    def json(self) -> Any:
        if self._raw is not None:
            import json as _json

            return _json.loads(self._raw.decode())  # raises on malformed
        return self._json


class _FakeAsyncClient:
    """Stand-in for ``httpx.AsyncClient`` — returns a scripted response or raises a scripted err."""

    script: Any = None  # a _FakeResp, or an Exception instance to raise on get()
    last_url: str | None = None
    last_headers: dict[str, str] | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResp:
        type(self).last_url = url
        type(self).last_headers = headers
        if isinstance(type(self).script, Exception):
            raise type(self).script
        assert isinstance(type(self).script, _FakeResp)
        return type(self).script


def _settings() -> Settings:
    return Settings(
        CLOUDPAYMENTS_API_BASE="https://pay.broadapps.dev/api/v1",
        CLOUDPAYMENTS_API_TOKEN="verify-token-secret-value",  # noqa: S106 - test-only
    )


@pytest.fixture
def _fake_httpx(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAsyncClient]:
    _FakeAsyncClient.script = None
    _FakeAsyncClient.last_url = None
    _FakeAsyncClient.last_headers = None
    monkeypatch.setattr(verify.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


@pytest.mark.asyncio
async def test_list_payments_returns_data_on_2xx(_fake_httpx: type[_FakeAsyncClient]) -> None:
    _fake_httpx.script = _FakeResp(200, json_body={"user_id": "d", "count": 1, "data": [_item()]})
    out = await CloudPaymentsVerifyClient(_settings()).list_payments(device_id=_DEVICE)
    assert len(out) == 1 and out[0]["payment_id"] == "pay-1"
    # SSRF-safe canonical device id in the path; Bearer carries our token to the fixed host.
    assert _fake_httpx.last_url == f"https://pay.broadapps.dev/api/v1/users/{_DEVICE}/payments"
    assert _fake_httpx.last_headers is not None
    assert _fake_httpx.last_headers["Authorization"] == "Bearer verify-token-secret-value"


@pytest.mark.asyncio
async def test_list_payments_filters_non_dict_items(_fake_httpx: type[_FakeAsyncClient]) -> None:
    _fake_httpx.script = _FakeResp(200, json_body={"data": [_item(), "junk", 5, None]})
    out = await CloudPaymentsVerifyClient(_settings()).list_payments(device_id=_DEVICE)
    assert len(out) == 1


@pytest.mark.asyncio
async def test_list_payments_404_is_empty_not_error(_fake_httpx: type[_FakeAsyncClient]) -> None:
    # 404 == "user has no payments" (permanent) -> [] (NOT a 500-retry).
    _fake_httpx.script = _FakeResp(404)
    out = await CloudPaymentsVerifyClient(_settings()).list_payments(device_id=_DEVICE)
    assert out == []


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [500, 502, 503, 400, 401, 403])
async def test_list_payments_non_2xx_is_unavailable(
    _fake_httpx: type[_FakeAsyncClient], code: int
) -> None:
    _fake_httpx.script = _FakeResp(code)
    with pytest.raises(CloudPaymentsVerificationUnavailableError):
        await CloudPaymentsVerifyClient(_settings()).list_payments(device_id=_DEVICE)


@pytest.mark.asyncio
async def test_list_payments_timeout_is_unavailable(_fake_httpx: type[_FakeAsyncClient]) -> None:
    _fake_httpx.script = httpx.TimeoutException("timed out")
    with pytest.raises(CloudPaymentsVerificationUnavailableError):
        await CloudPaymentsVerifyClient(_settings()).list_payments(device_id=_DEVICE)


@pytest.mark.asyncio
async def test_list_payments_connect_error_is_unavailable(
    _fake_httpx: type[_FakeAsyncClient],
) -> None:
    _fake_httpx.script = httpx.ConnectError("no route")
    with pytest.raises(CloudPaymentsVerificationUnavailableError):
        await CloudPaymentsVerifyClient(_settings()).list_payments(device_id=_DEVICE)


@pytest.mark.asyncio
async def test_list_payments_malformed_json_is_unavailable(
    _fake_httpx: type[_FakeAsyncClient],
) -> None:
    _fake_httpx.script = _FakeResp(200, raw=b"<<<not json>>>")
    with pytest.raises(CloudPaymentsVerificationUnavailableError):
        await CloudPaymentsVerifyClient(_settings()).list_payments(device_id=_DEVICE)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"count": 0}, {"data": "not-a-list"}, [1, 2, 3], "scalar"])
async def test_list_payments_missing_data_list_is_unavailable(
    _fake_httpx: type[_FakeAsyncClient], body: Any
) -> None:
    _fake_httpx.script = _FakeResp(200, json_body=body)
    with pytest.raises(CloudPaymentsVerificationUnavailableError):
        await CloudPaymentsVerifyClient(_settings()).list_payments(device_id=_DEVICE)
