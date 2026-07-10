"""Tests for TD-014: structured logging + metric on Anthropic upstream errors.

Contract: docs/modules/chat-orchestrator/03-architecture.md §Логирование upstream-ошибок
Anthropic; security: docs/05-security.md §Логирование; strategy: docs/06-testing-strategy.md.

`FakeAnthropicClient` (conftest) replaces the whole client and does NOT exercise the real
logging path, so these tests drive `_log_upstream_error` / `_extract_error_body` directly and the
real `AnthropicClient.create_message` with the SDK raising genuine anthropic exceptions
(`APIStatusError` / `APITimeoutError` / `APIConnectionError` / `AuthenticationError`), constructed
with their real signatures (anthropic 0.39.0: APIStatusError(message, *, response, body)).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import anthropic
import httpx
import pytest

from app.chat.anthropic_client import (
    AnthropicAuthError,
    AnthropicClient,
    _extract_error_body,
    _log_upstream_error,
)
from app.errors import UpstreamError
from app.observability.logging import JsonFormatter
from app.observability.metrics import anthropic_upstream_errors_total

# ----------------------------- exception factories (real SDK shapes) -----------------------------

_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(
    status: int,
    *,
    error_type: str | None = "invalid_request_error",
    error_message: str | None = "bad request",
    request_id: str | None = "req_abc123",
    body: Any = "__default__",
) -> anthropic.APIStatusError:
    headers = {"request-id": request_id} if request_id is not None else {}
    response = httpx.Response(status, headers=headers, request=_REQ)
    if body == "__default__":
        body = {"type": "error", "error": {"type": error_type, "message": error_message}}
    return anthropic.APIStatusError("upstream", response=response, body=body)


def _auth_error() -> anthropic.AuthenticationError:
    response = httpx.Response(401, headers={"request-id": "req_auth"}, request=_REQ)
    body = {
        "type": "error",
        "error": {"type": "authentication_error", "message": "invalid x-api-key"},
    }
    return anthropic.AuthenticationError("unauthorized", response=response, body=body)


# ----------------------------- log capture helper -----------------------------


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def captured_logs() -> Iterator[_Capture]:
    """Attach a capture handler to the app.chat.anthropic logger and reset the metric.

    Hermetic against cross-test logging pollution: a prior integration test that calls
    create_app() -> configure_logging() (which does root.handlers.clear()) combined with pytest's
    logging plugin can leave the `app.chat.anthropic` logger with `disabled=True`, which would
    silently drop our captured records (order-dependent flake). In production the logger is always
    enabled, so we force-enable it for the duration of the test and restore the flag after.
    """
    logger = logging.getLogger("app.chat.anthropic")
    handler = _Capture()
    prev_level = logger.level
    prev_disabled = logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    # Reset the metric so per-test increment assertions are deterministic.
    anthropic_upstream_errors_total.clear()
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)
        logger.disabled = prev_disabled


def _event(handler: _Capture) -> tuple[logging.LogRecord, dict[str, Any]]:
    """Return the single upstream-error record and its extra_fields dict."""
    recs = [r for r in handler.records if r.getMessage() == "anthropic_upstream_error"]
    assert len(recs) == 1, f"expected exactly one event, got {len(recs)}"
    rec = recs[0]
    fields = getattr(rec, "extra_fields", None)
    assert isinstance(fields, dict)
    return rec, fields


def _metric(status_code: str, error_type: str) -> float:
    return anthropic_upstream_errors_total.labels(
        status_code=status_code, error_type=error_type
    )._value.get()


# ============================= Scenario 1: 4xx (incl. 429) → WARNING =============================


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
def test_4xx_logs_warning_with_full_fields(captured_logs: _Capture, status: int) -> None:
    exc = _status_error(
        status,
        error_type="rate_limit_error" if status == 429 else "invalid_request_error",
        error_message="slow down" if status == 429 else "bad request",
        request_id="req_4xx",
    )
    _log_upstream_error(exc, model="claude-sonnet-4-5")
    rec, fields = _event(captured_logs)

    assert rec.levelno == logging.WARNING
    assert fields["event"] == "anthropic_upstream_error"
    assert fields["status_code"] == status
    assert fields["errorType"] == ("rate_limit_error" if status == 429 else "invalid_request_error")
    assert fields["errorMessage"] == ("slow down" if status == 429 else "bad request")
    assert fields["model"] == "claude-sonnet-4-5"
    assert fields["exceptionClass"] == "APIStatusError"
    assert fields["anthropicRequestId"] == "req_4xx"


def test_4xx_without_request_id_omits_field(captured_logs: _Capture) -> None:
    exc = _status_error(400, request_id=None)
    _log_upstream_error(exc, model="m")
    _rec, fields = _event(captured_logs)
    assert "anthropicRequestId" not in fields


# ============================= Scenario 2: 5xx → ERROR =============================


@pytest.mark.parametrize("status", [500, 502, 503, 529])
def test_5xx_logs_error_with_full_fields(captured_logs: _Capture, status: int) -> None:
    exc = _status_error(
        status, error_type="overloaded_error", error_message="overloaded", request_id="req_5xx"
    )
    _log_upstream_error(exc, model="claude-sonnet-4-5")
    rec, fields = _event(captured_logs)

    assert rec.levelno == logging.ERROR
    assert fields["status_code"] == status
    assert fields["errorType"] == "overloaded_error"
    assert fields["errorMessage"] == "overloaded"
    assert fields["exceptionClass"] == "APIStatusError"
    assert fields["anthropicRequestId"] == "req_5xx"


# =============== Scenario 3: timeout / connection → ERROR, no status_code ===============


@pytest.mark.parametrize(
    ("exc", "cls_name"),
    [
        (anthropic.APITimeoutError(request=_REQ), "APITimeoutError"),
        (anthropic.APIConnectionError(request=_REQ), "APIConnectionError"),
    ],
)
def test_network_errors_log_error_without_status(
    captured_logs: _Capture, exc: Exception, cls_name: str
) -> None:
    _log_upstream_error(exc, model="claude-sonnet-4-5")
    rec, fields = _event(captured_logs)

    assert rec.levelno == logging.ERROR
    assert "status_code" not in fields
    assert "errorType" not in fields
    assert "errorMessage" not in fields
    assert "anthropicRequestId" not in fields
    assert fields["exceptionClass"] == cls_name
    assert fields["model"] == "claude-sonnet-4-5"
    # Metric: status_code='none', error_type='unknown'.
    assert _metric("none", "unknown") == 1.0


# =============== Scenario 4: AuthenticationError (401) → WARNING + log ===============


def test_auth_error_logs_warning(captured_logs: _Capture) -> None:
    exc = _auth_error()
    _log_upstream_error(exc, model="claude-sonnet-4-5")
    rec, fields = _event(captured_logs)

    assert rec.levelno == logging.WARNING  # 401 is 4xx
    assert fields["status_code"] == 401
    assert fields["errorType"] == "authentication_error"
    assert fields["errorMessage"] == "invalid x-api-key"
    assert fields["exceptionClass"] == "AuthenticationError"
    assert fields["anthropicRequestId"] == "req_auth"


# =============== Scenario 5: redaction / security (CRITICAL) ===============


def test_no_secret_field_keys_in_logged_event(captured_logs: _Capture) -> None:
    """The event must not introduce any field whose key trips the redaction denylist
    (key/token/secret/...). All logged keys are non-sensitive metadata."""
    exc = _status_error(403, error_type="permission_error", error_message="forbidden")
    _log_upstream_error(exc, model="claude-sonnet-4-5")
    _rec, fields = _event(captured_logs)
    sensitive = ("key", "token", "secret", "password", "authorization", "credential")
    for field_name in fields:
        low = field_name.lower()
        assert not any(s in low for s in sensitive), f"sensitive-looking field logged: {field_name}"


def test_provider_error_message_survives_formatter_redaction(captured_logs: _Capture) -> None:
    """The provider error.message (e.g. 'This organization has been disabled.') is NOT a secret
    and must survive the JsonFormatter redaction pass; rendered JSON contains it verbatim."""
    msg = "This organization has been disabled."
    exc = _status_error(403, error_type="permission_error", error_message=msg)
    _log_upstream_error(exc, model="claude-sonnet-4-5")
    rec, _fields = _event(captured_logs)

    rendered = JsonFormatter().format(rec)
    payload = json.loads(rendered)
    assert payload["errorMessage"] == msg
    assert payload["errorType"] == "permission_error"
    assert payload["status_code"] == 403


def test_hypothetical_secret_field_would_be_redacted_by_formatter(captured_logs: _Capture) -> None:
    """Defense-in-depth: if a secret-named field ever reached the formatter, it would be redacted.
    Proves the same redaction middleware the event passes through cuts api-key/token shaped keys
    while keeping the provider errorMessage."""
    from app.observability.redaction import REDACTED

    logger = logging.getLogger("app.chat.anthropic")
    logger.warning(
        "anthropic_upstream_error",
        extra={
            "extra_fields": {
                "event": "anthropic_upstream_error",
                "errorMessage": "This organization has been disabled.",
                "apiKey": "sk-ant-should-never-appear",
                "anthropic_api_key": "sk-ant-service",
            }
        },
    )
    rec = [r for r in captured_logs.records if r.getMessage() == "anthropic_upstream_error"][-1]
    payload = json.loads(JsonFormatter().format(rec))
    assert payload["apiKey"] == REDACTED
    assert payload["anthropic_api_key"] == REDACTED
    assert "sk-ant-should-never-appear" not in json.dumps(payload)
    assert "sk-ant-service" not in json.dumps(payload)
    assert payload["errorMessage"] == "This organization has been disabled."


# =============== Scenario 7: resilience — malformed / absent body =================


@pytest.mark.parametrize(
    "body",
    [
        None,
        "raw-undecodable-string",
        123,
        [],
        {},  # no "error" key
        {"error": "not-a-dict"},
        {"error": {}},  # no type/message
        {"error": {"type": 42, "message": ["not", "a", "string"]}},  # non-string type/message
    ],
)
def test_extract_error_body_resilient(body: Any) -> None:
    exc = _status_error(500, body=body)
    error_type, error_message = _extract_error_body(exc)
    assert error_type is None
    assert error_message is None


def test_malformed_body_does_not_crash_logging(captured_logs: _Capture) -> None:
    exc = _status_error(500, body={"error": {"type": 42, "message": None}})
    _log_upstream_error(exc, model="m")  # must not raise
    _rec, fields = _event(captured_logs)
    assert "errorType" not in fields
    assert "errorMessage" not in fields
    assert fields["status_code"] == 500
    # Metric uses 'unknown' when error_type could not be extracted.
    assert _metric("500", "unknown") == 1.0


def test_extract_error_body_partial_only_type() -> None:
    exc = _status_error(400, body={"error": {"type": "invalid_request_error"}})
    error_type, error_message = _extract_error_body(exc)
    assert error_type == "invalid_request_error"
    assert error_message is None


# =============== Scenario 8: metric increments with bounded labels =================


def test_metric_status_and_type_from_body(captured_logs: _Capture) -> None:
    exc = _status_error(429, error_type="rate_limit_error", error_message="slow")
    _log_upstream_error(exc, model="m")
    assert _metric("429", "rate_limit_error") == 1.0


def test_metric_timeout_labels(captured_logs: _Capture) -> None:
    _log_upstream_error(anthropic.APITimeoutError(request=_REQ), model="m")
    assert _metric("none", "unknown") == 1.0


def test_metric_unknown_type_when_body_missing(captured_logs: _Capture) -> None:
    exc = _status_error(500, body=None)
    _log_upstream_error(exc, model="m")
    assert _metric("500", "unknown") == 1.0


# =============== Scenario 6: outward contract preserved (real create_message) =================


def _client_raising(exc: Exception) -> AnthropicClient:
    """Build a real AnthropicClient whose underlying SDK messages.create raises `exc`."""
    client = AnthropicClient()
    client._client.messages.create = AsyncMock(side_effect=exc)  # type: ignore[method-assign]
    return client


async def _run(client: AnthropicClient) -> None:
    await client.create_message(system_prompt="sys", messages=[], tools=[])


@pytest.mark.asyncio
async def test_status_error_maps_to_upstream_and_logs(captured_logs: _Capture) -> None:
    client = _client_raising(_status_error(503, error_type="overloaded_error", error_message="x"))
    with pytest.raises(UpstreamError):
        await _run(client)
    rec, fields = _event(captured_logs)
    assert rec.levelno == logging.ERROR
    assert fields["status_code"] == 503


@pytest.mark.asyncio
async def test_timeout_maps_to_upstream_and_logs(captured_logs: _Capture) -> None:
    client = _client_raising(anthropic.APITimeoutError(request=_REQ))
    with pytest.raises(UpstreamError):
        await _run(client)
    rec, fields = _event(captured_logs)
    assert rec.levelno == logging.ERROR
    assert "status_code" not in fields


@pytest.mark.asyncio
async def test_auth_error_maps_to_anthropic_auth_and_logs(captured_logs: _Capture) -> None:
    client = _client_raising(_auth_error())
    with pytest.raises(AnthropicAuthError):
        await _run(client)
    rec, fields = _event(captured_logs)
    assert rec.levelno == logging.WARNING
    assert fields["status_code"] == 401
    assert fields["exceptionClass"] == "AuthenticationError"


@pytest.mark.asyncio
async def test_upstream_error_does_not_leak_anthropic_details(captured_logs: _Capture) -> None:
    """Outward contract: the raised UpstreamError message is generic — no provider details, no
    api-key, no status leaks into the exception surfaced to the gateway (→ 502)."""
    secret_msg = "This organization has been disabled."
    client = _client_raising(
        _status_error(403, error_type="permission_error", error_message=secret_msg)
    )
    with pytest.raises(UpstreamError) as ei:
        await _run(client)
    assert secret_msg not in str(ei.value)
    assert "403" not in str(ei.value)
    assert "sk-ant" not in str(ei.value)


@pytest.mark.asyncio
async def test_log_emitted_exactly_once_on_create_message_path(captured_logs: _Capture) -> None:
    """The log call is BEFORE the mapping (so an upstream error always produces a log line first).
    Sanity: exactly one event is emitted on the real create_message path (no double-logging)."""
    client = _client_raising(_status_error(500))
    with pytest.raises(UpstreamError):
        await _run(client)
    events = [r for r in captured_logs.records if r.getMessage() == "anthropic_upstream_error"]
    assert len(events) == 1
