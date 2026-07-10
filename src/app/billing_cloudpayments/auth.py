"""Observational (non-blocking) dependency for the PUBLIC RU CloudPayments webhook (ADR-054 §1).

Diagnosis (ADR-052 §3 / ADR-054): broadapps sends the payment callback WITHOUT any authorization
(``authScheme=none`` — no ``Authorization``, no ``X-Api-Key``/signature). Requiring a token thus
means a permanent ``401`` and lost payments. Under ADR-054 the endpoint is PUBLIC: the trust anchor
is not the callback but the outgoing broadapps API verification (``verify.py``) done with our
``CLOUDPAYMENTS_API_TOKEN``; the instance-activation gate moved to that token, checked in
``service.handle()``.

``require_cloudpayments_webhook`` is kept ONLY as an observational dependency: it never raises. It
reads the raw ``Authorization`` header, computes ``matched`` (constant-time against the legacy
``CLOUDPAYMENTS_WEBHOOK_TOKEN`` if that secret is set — for the log only, it does NOT gate) and the
scheme word, emits one ``cloudpayments_webhook_auth_observed`` record (DEBUG/INFO) and passes the
request through. This keeps visibility if broadapps ever introduces a signature/header (Q-052-1)
and preserves the ``cloudPaymentsWebhook`` OpenAPI security scheme (lock icon) as decorative. The
secret/token value is never logged (only header names + the scheme word + the ``matched`` bool).
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.api_gateway.openapi_security import cloudpayments_webhook_scheme
from app.config import get_settings
from app.observability.logging import log_event

logger = logging.getLogger(__name__)  # == "app.billing_cloudpayments.auth"

# Fixed allowlist of header NAMES (never values) surfaced for diagnostics: lets us see if broadapps
# ever starts sending a secret in a different header / as a signature (ADR-052 §3, Q-052-1).
_AUTH_HEADER_ALLOWLIST = (
    "authorization",
    "x-api-key",
    "x-signature",
    "x-sign",
    "x-webhook-signature",
    "x-content-hmac",
    "content-hmac",
    "signature",
)


def _extract_webhook_credential(authorization: str | None) -> str | None:
    """Lenient extraction of a presented credential from a raw ``Authorization`` (ADR-052 §1).

    - ``None`` / blank (after strip) -> ``None``.
    - ``Bearer <token>`` / ``Token <token>`` (scheme word case-insensitive) -> the value after the
      scheme (stripped); empty remainder -> ``None``.
    - a single part (no whitespace) -> the whole trimmed header (raw ``<token>`` without a scheme).
    - an unrecognised scheme (``Basic xxx``) -> the whole trimmed header as-is.

    Used ONLY to compute the observational ``matched`` flag — it never gates the request.
    """
    if authorization is None:
        return None
    value = authorization.strip()
    if not value:
        return None
    parts = value.split(None, 1)  # split on the first run of whitespace
    if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
        rest = parts[1].strip()
        return rest or None
    return value


def _auth_scheme_label(authorization: str | None) -> str:
    """Return the scheme WORD only (never the token value) for the observational log (ADR-052 §3).

    ``None`` -> ``"none"`` (expected for broadapps); blank -> ``"empty"``; a single part (raw token,
    no whitespace) -> ``"raw"``; otherwise the first word lower-cased (``"bearer"``/``"token"``/…).
    """
    if authorization is None:
        return "none"
    value = authorization.strip()
    if not value:
        return "empty"
    parts = value.split(None, 1)
    return parts[0].lower() if len(parts) == 2 else "raw"


def _log_auth_observed(request: Request) -> None:
    """Emit one non-blocking DEBUG/INFO ``cloudpayments_webhook_auth_observed`` record (ADR-054 §1).

    Strict allowlist: ``matched`` (optional legacy-token match, log-only), ``authScheme`` (scheme
    word only) and ``presentAuthHeaders`` (NAMES of present headers from the fixed allowlist). NEVER
    the token value, the full ``Authorization`` header, any header value, or the body.
    """
    header = request.headers.get("authorization")
    secret = get_settings().cloudpayments_webhook_token
    # matched is purely informational (does NOT gate). Only compute a real compare when the legacy
    # secret is configured; otherwise there is nothing to match against.
    if secret:
        candidate = _extract_webhook_credential(header) or ""
        matched = hmac.compare_digest(candidate, secret)
    else:
        matched = False
    present = [name for name in _AUTH_HEADER_ALLOWLIST if name in request.headers]
    log_event(
        logger,
        logging.INFO,
        "cloudpayments_webhook_auth_observed",
        matched=matched,
        authScheme=_auth_scheme_label(header),
        presentAuthHeaders=present,
    )


def require_cloudpayments_webhook(
    request: Request,
    _scheme: Annotated[
        HTTPAuthorizationCredentials | None, Depends(cloudpayments_webhook_scheme)
    ] = None,
) -> None:
    """Observational, non-blocking auth dependency for the PUBLIC webhook (ADR-054 §1).

    NEVER raises: broadapps sends the callback without authorization, so any token requirement would
    mean a permanent 401 and lost payments. It only records one observational
    ``cloudpayments_webhook_auth_observed`` log. ``_scheme`` is a decorative ``SecurityBase``
    dependency that keeps the ``cloudPaymentsWebhook`` scheme (lock icon / Authorize) in OpenAPI;
    its extracted credential is NOT used. The real trust anchor is the broadapps API verification
    in ``service.handle()``.
    """
    _log_auth_observed(request)
