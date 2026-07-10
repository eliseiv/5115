"""Per-route bearer authorization for the Adapty webhook (ADR-029 §1, billing-adapty/03).

Isolated, constant-time bearer check — modelled on ``require_admin`` (``api_gateway/auth.py``) but
using its own secret ``ADAPTY_WEBHOOK_SECRET`` (separate from JWT / admin / KMS / preview, and
per-instance under ADR-017). Implemented as a per-route ``Depends``, NOT a global middleware: the
endpoint is fully isolated from the user JWT chain and from the admin token.

Semantics:
- secret not configured (``ADAPTY_WEBHOOK_SECRET == ""``) -> 500 (misconfiguration, clear text).
  A blank configured secret never matches any presented token.
- missing / non-Bearer / mismatching token -> 401 (without revealing the reason).
The secret is never logged (the ``authorization`` header is covered by the redaction denylist).
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials

from app.api_gateway.openapi_security import adapty_webhook_scheme
from app.config import get_settings
from app.errors import ServiceUnavailableError, UnauthorizedError


class AdaptyWebhookMisconfiguredError(ServiceUnavailableError):
    """``ADAPTY_WEBHOOK_SECRET`` is not configured — the endpoint cannot authenticate (ADR-029).

    Mapped to HTTP 500 (not 503): the contract (billing-adapty/02-api-contracts.md) specifies a
    500 misconfiguration response so Adapty retries until the operator sets the secret. The clear
    message names the missing configuration without leaking any secret material.
    """

    status_code = 500
    code = "adapty_webhook_misconfigured"


def require_adapty_webhook(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(adapty_webhook_scheme)
    ] = None,
) -> None:
    """Authorize an Adapty webhook call via the isolated static bearer secret (ADR-029 §1).

    The token value comes from ``adapty_webhook_scheme`` (HTTPBearer, ``auto_error=False``): a
    ``SecurityBase`` that contributes the ``adaptyWebhook`` scheme to OpenAPI (Authorize) without
    raising on a missing/malformed header, so the 500-on-unset / 401-on-mismatch behaviour below
    is the single source of truth. Comparison is constant-time (``hmac.compare_digest``).
    """
    secret = get_settings().adapty_webhook_secret
    if not secret:
        raise AdaptyWebhookMisconfiguredError("adapty webhook secret not configured")
    presented = credentials.credentials if credentials is not None else None
    if presented is None or not hmac.compare_digest(presented, secret):
        raise UnauthorizedError("invalid adapty webhook token")
