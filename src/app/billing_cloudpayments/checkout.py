"""Outgoing broadapps ``/payments/link`` call that creates a RU payment link (ADR-051 §3,
billing-cloudpayments/03 §Checkout).

``CloudPaymentsCheckoutClient`` performs a single per-call ``httpx.AsyncClient`` POST to
``{settings.cloudpayments_api_base}/payments/link`` with a ``multipart/form-data`` body (via
``files=``, NOT ``data=``) and a server-held ``Authorization: Bearer <api_token>``. Every upstream
failure (timeout / connect / non-2xx / malformed body) is mapped to a generic ``UpstreamError``
(502) that NEVER leaks the upstream body/status or our token to the client. Exactly one structured
log ``"cloudpayments_checkout_outcome"`` is emitted per call with an allowlist of fields —
``customer_email`` (PII), the Bearer token and ``app_id`` are never logged (ADR-051 §6).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.billing_cloudpayments.parser import KIND_TOKENS, KIND_UNKNOWN, classify_product
from app.config import Settings
from app.errors import UpstreamError, ValidationFailedError
from app.observability.logging import log_event

logger = logging.getLogger(__name__)  # == "app.billing_cloudpayments.checkout"

# Connect+read timeout for the outgoing broadapps call (ADR-051 §3). No dedicated env — the three
# CLOUDPAYMENTS_API_* configs are sufficient.
_CHECKOUT_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class CheckoutResult:
    """Passthrough of the broadapps payment-link response (ADR-051 §4)."""

    payment_id: str
    payment_url: str
    status: str
    expires_at: str | None


class CloudPaymentsCheckoutClient:
    """Creates a RU payment link via broadapps. Passthrough — no DB, no persisted state."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def validate_product(self, product_id: str) -> None:
        """Allowlist gate symmetric with the webhook (ADR-051 §2, 03 §Валидация productId).

        Reuses ``classify_product`` (billing_interval_unit unknown at checkout => None): we only
        issue a link for a product the webhook could later credit. Does NOT size any grant.
        ``unknown`` OR a ``tokens`` product with a non-positive credit value => 422.
        """
        token_products = self._settings.token_products()
        kind = classify_product(product_id, None, frozenset(token_products))
        if kind == KIND_UNKNOWN:
            raise ValidationFailedError("unknown_product")
        if kind == KIND_TOKENS and token_products.get(product_id, 0) <= 0:
            raise ValidationFailedError("unknown_product")

    async def create_payment_link(
        self, *, user_id: uuid.UUID, product_id: str, customer_email: str
    ) -> CheckoutResult:
        """POST broadapps ``/payments/link`` and return the created link (ADR-051 §3).

        ``user_id`` is the authenticated subject (from JWT ``sub``), never a client-supplied value.
        Any upstream failure maps to ``UpstreamError`` (502) without leaking upstream detail/token.
        """
        settings = self._settings
        url = f"{settings.cloudpayments_api_base}/payments/link"
        # multipart/form-data via files= with (None, value) tuples — httpx sets the Content-Type
        # boundary itself. Do NOT set Content-Type by hand, and do NOT use data= (urlencoded).
        files: dict[str, tuple[None, str]] = {
            "app_id": (None, settings.cloudpayments_app_id),
            "product_id": (None, product_id),
            "user_id": (None, str(user_id)),
            "customer_email": (None, customer_email),
        }
        headers = {
            "Authorization": f"Bearer {settings.cloudpayments_api_token}",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=_CHECKOUT_TIMEOUT_SECONDS) as client:
                response = await client.post(url, files=files, headers=headers)
        except httpx.TimeoutException as exc:
            raise self._upstream_error("timeout", user_id=user_id, product_id=product_id) from exc
        except httpx.RequestError as exc:
            raise self._upstream_error(
                "connect_error", user_id=user_id, product_id=product_id
            ) from exc

        # broadapps success = 201; accept any 2xx defensively. A non-2xx never proxies the upstream
        # status/body outward — only a generic 502.
        if not (200 <= response.status_code < 300):
            raise self._upstream_error("upstream_status", user_id=user_id, product_id=product_id)

        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._upstream_error(
                "malformed_response", user_id=user_id, product_id=product_id
            ) from exc

        result = self._to_result(body)
        if result is None:
            raise self._upstream_error("malformed_response", user_id=user_id, product_id=product_id)

        log_event(
            logger,
            logging.INFO,
            "cloudpayments_checkout_outcome",
            result="created",
            userId=str(user_id),
            productId=product_id,
            status=result.status,
            paymentId=result.payment_id,
        )
        return result

    @staticmethod
    def _to_result(body: Any) -> CheckoutResult | None:
        """Project the broadapps JSON body into a ``CheckoutResult`` or None if malformed.

        Requires a non-empty ``payment_url``; ``expires_at`` is passed through as a string or None
        without parsing (upstream format is not fixed — passthrough is safer, ADR-051 §4).
        """
        if not isinstance(body, dict):
            return None
        payment_url = body.get("payment_url")
        if not isinstance(payment_url, str) or not payment_url:
            return None
        payment_id = body.get("payment_id")
        status = body.get("status")
        expires_at = body.get("expires_at")
        return CheckoutResult(
            payment_id=str(payment_id) if payment_id is not None else "",
            payment_url=payment_url,
            status=str(status) if status is not None else "",
            expires_at=expires_at if isinstance(expires_at, str) else None,
        )

    def _upstream_error(self, reason: str, *, user_id: uuid.UUID, product_id: str) -> UpstreamError:
        """Log the error outcome (allowlist) and build the generic 502 raised to the client."""
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_checkout_outcome",
            result="error",
            reason=reason,
            userId=str(user_id),
            productId=product_id,
        )
        return UpstreamError("payment provider unavailable")
