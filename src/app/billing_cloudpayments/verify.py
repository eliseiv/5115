"""Outgoing broadapps payment verification + pure reconciliation (ADR-054 §2/§Верификация).

The RU CloudPayments callback carries no signature and no auth, so it is only a TRIGGER. The single
trusted "payment happened" signal is the broadapps API, queried with our API token.

- :class:`CloudPaymentsVerifyClient` performs a per-call ``httpx.AsyncClient`` ``GET
  {api_base}/users/{deviceId}/payments`` and maps every transient failure (timeout / connect / 5xx /
  malformed) to :class:`CloudPaymentsVerificationUnavailableError` (500, retriable). A broadapps
  ``404`` means "no payments" (permanent) and returns ``[]`` — NOT a 500-retry. The upstream body
  and our token are never proxied outward or logged.
- :func:`select_creditable_payments` is a PURE reconciliation: keep only payments whose ``status``
  is in the configured paid-set AND whose ``paid_at`` is within the freshness window AND that carry
  a valid ``payment_id`` / ``product.code`` / ``product.payment_type``.

The device id in the path is an already-validated ``uuid.UUID`` (SSRF-safe — canonical ``str`` from
a parsed UUID, never taken from the callback body); the host comes from config.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.errors import CloudPaymentsVerificationUnavailableError
from app.observability.logging import log_event

logger = logging.getLogger(__name__)  # == "app.billing_cloudpayments.verify"

# Connect+read timeout for the outgoing broadapps verification GET (ADR-054 §2). Same value as the
# checkout client (ADR-051) — a short partner-API budget.
_VERIFY_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class CreditablePayment:
    """A broadapps payment that passed reconciliation and is ready to credit (ADR-054 §6/§7).

    ``payment_id`` is the stable broadapps id used both for the dedup journal key and the ledger
    idempotency key (``cp-txn:{payment_id}``). ``payment_type`` is the authoritative class
    (``one_time`` -> tokens, ``subscription`` -> subscription); ``product_code`` maps to the
    server-side credit tables (anti-tamper — the amount never comes from the broadapps ``amount``).
    """

    payment_id: str
    product_code: str
    payment_type: str
    status: str
    paid_at: datetime.datetime


class CloudPaymentsVerifyClient:
    """Queries broadapps for a device's payments. Stateless — needs only settings (no DB)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def list_payments(self, *, device_id: uuid.UUID) -> list[dict[str, Any]]:
        """``GET {api_base}/users/{device_id}/payments`` and return the raw ``data`` list (ADR-054).

        ``device_id`` is a validated ``uuid.UUID`` -> ``str(device_id)`` is canonical (SSRF-safe).
        Outcomes:
        - 2xx + a JSON object with a ``data`` list -> the list of ``dict`` items.
        - ``404`` -> ``[]`` ("no payments", permanent — NOT a transient error; Q-054-2).
        - timeout / connect / non-2xx (except 404) / non-JSON / no ``data`` list ->
          :class:`CloudPaymentsVerificationUnavailableError` (500, retriable). The upstream body/
          status and our Bearer token are never proxied outward or logged.
        """
        settings = self._settings
        url = f"{settings.cloudpayments_api_base}/users/{device_id}/payments"
        headers = {
            "Authorization": f"Bearer {settings.cloudpayments_api_token}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=_VERIFY_TIMEOUT_SECONDS) as client:
                response = await client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise self._unavailable("timeout") from exc
        except httpx.RequestError as exc:
            raise self._unavailable("connect_error") from exc

        # 404 = "user has no payments" (permanent) -> empty list, NOT api_error (ADR-054 §2).
        if response.status_code == 404:
            return []
        if not (200 <= response.status_code < 300):
            raise self._unavailable("upstream_status")

        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._unavailable("malformed_response") from exc
        if not isinstance(body, dict):
            raise self._unavailable("malformed_response")
        data = body.get("data")
        if not isinstance(data, list):
            raise self._unavailable("malformed_response")
        return [item for item in data if isinstance(item, dict)]

    def _unavailable(self, reason: str) -> CloudPaymentsVerificationUnavailableError:
        """Log the transient verify failure (allowlist: no token/body); build the 500 to raise."""
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_verify_outcome",
            verify="api_error",
            reason=reason,
        )
        return CloudPaymentsVerificationUnavailableError("cloudpayments verification unavailable")


def _parse_paid_at(value: Any) -> datetime.datetime | None:
    """Parse a broadapps ``paid_at`` (ISO-8601, e.g. ``2026-04-11T08:52:46+00:00``) -> aware UTC.

    Tolerates a trailing ``Z``. A naive result is assumed UTC so it compares against the aware
    ``now``. Non-string / unparseable -> ``None`` (the payment is then skipped, never credited).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def payment_statuses(data: list[dict[str, Any]]) -> list[str]:
    """The raw broadapps ``status`` strings from ``data[]`` for the outcome log (ADR-054 §5).

    Safe to log (not PII, not a secret) — used to calibrate CLOUDPAYMENTS_PAID_STATUSES from prod.
    """
    return [str(item.get("status")) for item in data if isinstance(item, dict)]


def select_creditable_payments(
    data: list[dict[str, Any]],
    *,
    paid_statuses: frozenset[str],
    now: datetime.datetime,
    freshness_hours: int,
) -> list[CreditablePayment]:
    """Pure reconciliation: pick creditable payments from a broadapps ``data`` list (ADR-054 §6).

    A payment is kept iff ALL hold:
    - ``str(p["status"]).strip().lower()`` is in ``paid_statuses`` (default ``{"succeeded"}``);
    - ``paid_at >= now - timedelta(hours=freshness_hours)`` (freshness window, reference = ``now``);
    - it carries a non-empty ``payment_id`` and ``product.code`` and ``product.payment_type``.

    ``now`` must be timezone-aware (UTC). No I/O, no mutation — testable in isolation.
    """
    cutoff = now - datetime.timedelta(hours=freshness_hours)
    creditable: list[CreditablePayment] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in paid_statuses:
            continue
        paid_at = _parse_paid_at(item.get("paid_at"))
        if paid_at is None or paid_at < cutoff:
            continue
        payment_id = item.get("payment_id")
        if not isinstance(payment_id, str) or not payment_id.strip():
            continue
        product = item.get("product")
        if not isinstance(product, dict):
            continue
        product_code = product.get("code")
        payment_type = product.get("payment_type")
        if not isinstance(product_code, str) or not product_code.strip():
            continue
        if not isinstance(payment_type, str) or not payment_type.strip():
            continue
        creditable.append(
            CreditablePayment(
                payment_id=payment_id.strip(),
                product_code=product_code.strip(),
                payment_type=payment_type.strip().lower(),
                status=status,
                paid_at=paid_at,
            )
        )
    return creditable
