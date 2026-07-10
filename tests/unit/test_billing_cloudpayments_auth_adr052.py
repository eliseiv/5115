"""Unit: ADR-054 — PUBLIC CloudPayments webhook auth is OBSERVATIONAL (never raises).

Supersedes the ADR-052 blocking-auth unit suite. Under ADR-054 the endpoint is public (broadapps
sends the callback with ``authScheme=none``), so ``require_cloudpayments_webhook`` NEVER raises a
401/500 — the config-activation gate (missing ``CLOUDPAYMENTS_API_TOKEN`` -> 500) moved into
``service.handle()`` and ``CloudPaymentsWebhookMisconfiguredError`` now lives in ``app.errors``.
This module asserts the remaining behaviour of ``auth.py``:

- ``require_cloudpayments_webhook`` returns ``None`` (passes through) for EVERY header shape — valid
  legacy token, wrong token, raw, unknown scheme, and no header at all — and raises nothing.
- Each call emits EXACTLY ONE ``cloudpayments_webhook_auth_observed`` record whose fields are only
  the allowlist (``matched`` bool + scheme WORD + present header NAMES). The secret/token value and
  the full ``Authorization`` header are never rendered.
- ``matched`` is purely informational: it is ``True`` only when the OPTIONAL legacy secret is set
  AND the presented credential equals it; it does NOT gate.
- The pure helpers ``_extract_webhook_credential`` / ``_auth_scheme_label`` never surface the value.

Fully hermetic: NO DB, NO network, NO LLM; the optional legacy secret is injected via
``CLOUDPAYMENTS_WEBHOOK_TOKEN`` + ``get_settings.cache_clear()`` (restored afterwards).
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from starlette.datastructures import Headers

# ADR-054: the misconfigured-webhook error now lives in app.errors, not in auth.py; the config gate
# is enforced in service.handle(), NOT in this observational dependency.
from app.billing_cloudpayments.auth import (
    _auth_scheme_label,
    _extract_webhook_credential,
    require_cloudpayments_webhook,
)
from app.config import get_settings
from app.errors import CloudPaymentsWebhookMisconfiguredError
from app.observability.logging import JsonFormatter

_SECRET = "cloudpayments-webhook-secret-value-64chars-XXXXXXXXXXXXXXXXXXXXXXXX"
_MESSAGE = "cloudpayments_webhook_auth_observed"
_LOGGER = "app.billing_cloudpayments.auth"


def _request(headers: dict[str, str]) -> Request:
    """A minimal stand-in for ``Request`` exposing only ``.headers`` (all the verifier reads)."""
    return cast(Request, SimpleNamespace(headers=Headers(headers)))


@pytest.fixture
def _secret_set(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Configure the OPTIONAL legacy secret (for the ``matched`` flag only; restored after)."""
    monkeypatch.setenv("CLOUDPAYMENTS_WEBHOOK_TOKEN", _SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _secret_unset(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("CLOUDPAYMENTS_WEBHOOK_TOKEN", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _observed(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == _MESSAGE]


def _rendered(record: logging.LogRecord) -> dict[str, Any]:
    """Render through the REAL ``JsonFormatter`` — the exact line operators would see."""
    return cast(dict[str, Any], json.loads(JsonFormatter().format(record)))


# ============================ Public: NEVER raises ============================


@pytest.mark.parametrize(
    "headers",
    [
        {"authorization": f"Bearer {_SECRET}"},  # valid legacy token
        {"authorization": f"Bearer {_SECRET}-wrong"},  # wrong token
        {"authorization": f"{_SECRET}-wrong"},  # raw wrong token
        {"authorization": f"Basic {_SECRET}"},  # unrecognised scheme
        {"authorization": ""},  # blank header
        {},  # broadapps reality: NO Authorization at all (authScheme=none)
    ],
)
def test_never_raises_for_any_header_when_secret_set(
    _secret_set: Any, headers: dict[str, str]
) -> None:
    # Public endpoint: the dependency passes through for EVERY shape and returns None.
    assert require_cloudpayments_webhook(_request(headers)) is None


def test_never_raises_when_legacy_secret_unset(_secret_unset: Any) -> None:
    # The activation gate moved to service.handle(); the observational dep never 500s here.
    assert require_cloudpayments_webhook(_request({})) is None
    assert require_cloudpayments_webhook(_request({"authorization": f"Bearer {_SECRET}"})) is None


def test_misconfigured_error_is_a_500_and_lives_in_errors() -> None:
    # Contract anchor: the moved error is a 500 with the documented code (raised in the service).
    err = CloudPaymentsWebhookMisconfiguredError("cloudpayments api token not configured")
    assert err.status_code == 500
    assert err.code == "cloudpayments_webhook_misconfigured"


# ============================ Exactly one observed log; allowlist only ============================


def test_observed_log_emitted_once_matched_true_on_valid_secret(
    _secret_set: Any, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger(_LOGGER).disabled = False
    caplog.set_level(logging.DEBUG)

    require_cloudpayments_webhook(
        _request({"authorization": f"Bearer {_SECRET}", "x-api-key": "secret-key-value"})
    )

    recs = _observed(caplog)
    assert len(recs) == 1, "exactly one auth_observed record per call"
    fields = _rendered(recs[0])
    assert fields["message"] == _MESSAGE
    assert fields["matched"] is True  # optional legacy token happens to match (log-only)
    assert fields["authScheme"] == "bearer"
    assert set(fields["presentAuthHeaders"]) == {"authorization", "x-api-key"}

    blob = json.dumps(fields)
    assert _SECRET not in blob  # token VALUE never rendered
    assert "secret-key-value" not in blob  # x-api-key VALUE never rendered, only its NAME


def test_observed_log_matched_false_on_wrong_token(
    _secret_set: Any, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger(_LOGGER).disabled = False
    caplog.set_level(logging.DEBUG)
    wrong = f"{_SECRET}-wrong"
    require_cloudpayments_webhook(_request({"authorization": f"Bearer {wrong}"}))
    fields = _rendered(_observed(caplog)[0])
    assert fields["matched"] is False
    assert wrong not in json.dumps(fields)


def test_observed_log_matched_false_when_no_legacy_secret(
    _secret_unset: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # No legacy secret configured -> nothing to match against -> matched=False (never gates).
    logging.getLogger(_LOGGER).disabled = False
    caplog.set_level(logging.DEBUG)
    require_cloudpayments_webhook(_request({"authorization": f"Bearer {_SECRET}"}))
    assert _rendered(_observed(caplog)[0])["matched"] is False


@pytest.mark.parametrize(
    "headers, scheme",
    [
        ({}, "none"),  # broadapps reality
        ({"authorization": ""}, "empty"),
        ({"authorization": f"{_SECRET}"}, "raw"),
        ({"authorization": f"Basic {_SECRET}"}, "basic"),
        ({"authorization": f"Token {_SECRET}"}, "token"),
    ],
)
def test_observed_log_auth_scheme_variants(
    _secret_set: Any, caplog: pytest.LogCaptureFixture, headers: dict[str, str], scheme: str
) -> None:
    logging.getLogger(_LOGGER).disabled = False
    caplog.set_level(logging.DEBUG)
    require_cloudpayments_webhook(_request(headers))
    recs = _observed(caplog)
    assert len(recs) == 1
    fields = _rendered(recs[0])
    assert fields["authScheme"] == scheme
    # The scheme word never carries the token value.
    assert _SECRET not in json.dumps(fields)


def test_observed_log_no_auth_header_is_authscheme_none(
    _secret_set: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # The canonical broadapps case: no Authorization -> authScheme=none, presentAuthHeaders=[].
    logging.getLogger(_LOGGER).disabled = False
    caplog.set_level(logging.DEBUG)
    require_cloudpayments_webhook(_request({}))
    fields = _rendered(_observed(caplog)[0])
    assert fields["authScheme"] == "none"
    assert fields["presentAuthHeaders"] == []


# ============================ Pure helpers (value never surfaces) ============================


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),  # case-insensitive scheme word
        ("Token abc123", "abc123"),
        ("token abc123", "abc123"),
        ("abc123", "abc123"),  # single part -> whole trimmed header (raw token)
        ("  abc123  ", "abc123"),
        ("  Bearer   abc123  ", "abc123"),
        (None, None),
        ("", None),
        ("   ", None),
        ("Bearer ", "Bearer"),  # trailing space collapses to one word -> raw "Bearer"
        ("Basic abc123", "Basic abc123"),  # unrecognised scheme -> whole header
    ],
)
def test_extract_webhook_credential(raw: str | None, expected: str | None) -> None:
    assert _extract_webhook_credential(raw) == expected


@pytest.mark.parametrize(
    "raw, label",
    [
        (None, "none"),
        ("", "empty"),
        ("   ", "empty"),
        ("Bearer x", "bearer"),
        ("bearer x", "bearer"),
        ("Token x", "token"),
        ("Basic x", "basic"),
        ("abc123", "raw"),
        ("  abc123  ", "raw"),
    ],
)
def test_auth_scheme_label_never_leaks_value(raw: str | None, label: str) -> None:
    result = _auth_scheme_label(raw)
    assert result == label
    assert "abc123" not in result
