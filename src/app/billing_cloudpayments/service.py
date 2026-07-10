"""CloudPaymentsWebhookService: callback = trigger -> broadapps verification -> reconcile -> credit.

ADR-054 (revises ADR-050). broadapps sends the RU payment callback WITHOUT auth or signature, so the
body is never trusted for crediting. ``handle(raw)`` treats the callback as a TRIGGER: it resolves
the deviceId to our userId (ADR-053), then VERIFIES the device's payments via the broadapps API
(``GET /users/{deviceId}/payments`` with our ``CLOUDPAYMENTS_API_TOKEN``) and credits every
confirmed-``succeeded`` payment inside the freshness window, idempotently by the broadapps
``payment_id``. Any malformed / non-completed / unknown callback yields an ``ignored`` outcome (the
router maps every outcome to HTTP 200 ``{"code": 0}``); only a real failure raises:
``CloudPaymentsWebhookMisconfiguredError`` (no API token) and
``CloudPaymentsVerificationUnavailableError`` (transient verify failure) surface as 500 so the
aggregator re-delivers.

Idempotency = the broadapps ``payment_id`` (ADR-054 §3), NOT the callback ``TransactionId``: one
callback reconciles MANY payments, so the key must be per-payment. Two layers: event-delivery dedup
via ``INSERT cloudpayments_webhook_events (transaction_id = payment_id) ON CONFLICT DO NOTHING
RETURNING`` (the ``transaction_id`` column is repurposed to hold ``payment_id`` — no migration,
Q-054-3), and grant idempotency via ``cp-txn:{payment_id}``. Each payment is credited in its OWN
short transaction so per-payment isolation holds; the outgoing verification GET runs OUTSIDE any
open DB transaction.

Anti-tamper (ADR-054 §6, ADR-015): the credit amount comes ONLY from server-side maps keyed by
``product.code`` — never from the broadapps ``amount``. The class comes from the authoritative
``product.payment_type`` (``one_time`` -> tokens, ``subscription`` -> subscription). Card data, the
bearer and the raw payload/body are never read into business logic, logged, or persisted.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_CLOUDPAYMENTS_PAYMENT, AuditEvent, AuditService
from app.billing_cloudpayments import parser, verify
from app.billing_cloudpayments.verify import CloudPaymentsVerifyClient, CreditablePayment
from app.config import Settings
from app.errors import CloudPaymentsWebhookMisconfiguredError
from app.observability.logging import log_event
from app.wallet.service import WalletService

logger = logging.getLogger(__name__)  # == "app.billing_cloudpayments.service"


@dataclass(frozen=True)
class WebhookOutcome:
    """Result of handling one webhook call. The router maps it to an HTTP-200 ``{"code": 0}``."""

    result: str  # "ignored" | "duplicate" | "applied"
    reason: str | None = None


def _ignored(reason: str) -> WebhookOutcome:
    return WebhookOutcome(result="ignored", reason=reason)


def _level_for(result: str, reason: str | None) -> int:
    """Map a webhook outcome to a log level (ADR-054 §5 / 08-observability.md).

    ``user_not_found`` / ``no_creditable_payment`` are WARNING (a callback arrived but nothing could
    be credited — the incident class the operator watches). ``applied`` / ``duplicate`` and the
    parse-shape ``ignored`` reasons are INFO; a fully empty body is a low-signal probe -> DEBUG.
    """
    if result in ("applied", "duplicate"):
        return logging.INFO
    # result == "ignored"
    if reason in ("user_not_found", "no_creditable_payment"):
        return logging.WARNING
    if reason == "empty_body":
        return logging.DEBUG
    # invalid_json | not_an_object | not_a_completed_payment | invalid_account_id
    return logging.INFO


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class CloudPaymentsWebhookService:
    def __init__(
        self,
        session: AsyncSession,
        wallet: WalletService,
        audit: AuditService,
        settings: Settings,
        verify_client: CloudPaymentsVerifyClient,
    ) -> None:
        self._session = session
        self._wallet = wallet
        self._audit = audit
        self._settings = settings
        self._verify = verify_client

    def _log_outcome(
        self,
        outcome: WebhookOutcome,
        *,
        transaction_id: str | None = None,
        user_id: uuid.UUID | None = None,
        resolved_via: str | None = None,
        verify_result: str | None = None,
        credited_count: int | None = None,
        payment_statuses: list[str] | None = None,
    ) -> WebhookOutcome:
        """Emit exactly one structured ``cloudpayments_webhook_outcome`` record; return ``outcome``.

        Only the fixed allowlist is logged (ADR-054 §5 / 08-observability.md): result / reason /
        transactionId (callback ``TransactionId``, log context only) / userId (the RESOLVED internal
        UUID -> ``str``) / resolvedVia / verify / creditedCount / paymentStatuses. ``None`` fields
        are dropped by the JsonFormatter. Verification fields appear only on post-verify outcomes.
        Card data, the bearer, ``amount``/``currency`` and the raw payload/verify body are never
        logged.
        """
        level = _level_for(outcome.result, outcome.reason)
        log_event(
            logger,
            level,
            "cloudpayments_webhook_outcome",
            result=outcome.result,
            reason=outcome.reason,
            transactionId=transaction_id,
            userId=str(user_id) if user_id is not None else None,
            resolvedVia=resolved_via,
            verify=verify_result,
            creditedCount=credited_count,
            paymentStatuses=payment_statuses,
        )
        return outcome

    async def handle(self, raw: bytes) -> WebhookOutcome:
        """Process one raw callback: trigger -> resolve -> verify -> reconcile -> credit (ADR-054).

        Order (an early ``ignored`` before any outgoing GET, so a forged callback cannot use the
        broadapps API as an oracle/amplifier):
        0. config gate: no ``CLOUDPAYMENTS_API_TOKEN`` -> 500 misconfigured (cannot verify).
        1. body shape (empty / not-JSON / not-object) -> ``ignored`` (no DB, no GET).
        2. gate ``Status=Completed & OperationType=Payment`` + deviceId ``X`` (UUID, lower). The
           callback ``TransactionId``/``product_id`` are optional log context only — never gate.
        3. resolve ``X`` -> our userId (ADR-053); miss -> ``user_not_found`` (WARNING, NO GET).
        4. verify: ``GET /users/{X}/payments`` (outside any open DB tx). Transient failure raises
           ``CloudPaymentsVerificationUnavailableError`` (500, retriable).
        5. reconcile (pure): keep ``succeeded`` payments inside the freshness window; empty ->
           ``no_creditable_payment`` (WARNING).
        6. credit each selected payment in its own tx; aggregate -> ``applied`` / ``duplicate``.
        """
        # --- Stage 0: config gate (ADR-054 §1) ---
        if not self._settings.cloudpayments_api_token:
            raise CloudPaymentsWebhookMisconfiguredError("cloudpayments api token not configured")

        # --- Stage 1: body shape (no DB, no GET) ---
        if not raw:
            return self._log_outcome(_ignored("empty_body"))
        try:
            body = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return self._log_outcome(_ignored("invalid_json"))
        if not isinstance(body, dict):
            return self._log_outcome(_ignored("not_an_object"))

        # --- Stage 2: gate + deviceId (TransactionId/product_id are optional log context now) ---
        status = parser.parse_status(body)
        operation_type = parser.parse_operation_type(body)
        transaction_id = parser.parse_transaction_id(body)  # log context only (ADR-054 §2)
        if not parser.parse_gate(status, operation_type):
            return self._log_outcome(
                _ignored("not_a_completed_payment"), transaction_id=transaction_id
            )

        data = parser._parse_data(body) or {}
        device_id = parser.parse_user_id(body, data)
        if device_id is None:
            return self._log_outcome(_ignored("invalid_account_id"), transaction_id=transaction_id)

        # --- Stage 3: two-step user resolution (ADR-053; DB read; never provision users here) ---
        resolved = await self._resolve_user(device_id)
        if resolved is None:
            # X is only a candidate deviceId (not our confirmed user) -> userId omitted, NO GET.
            return self._log_outcome(_ignored("user_not_found"), transaction_id=transaction_id)
        resolved_user_id, resolved_via = resolved

        # Close the read transaction opened by the resolution SELECTs BEFORE the outgoing GET, so
        # the 15s network call never holds a DB transaction/connection open (ADR-054 §2).
        await self._session.commit()

        # --- Stage 4: verification (outgoing GET; transient failure -> 500 retriable) ---
        payments_data = await self._verify.list_payments(device_id=device_id)
        statuses = verify.payment_statuses(payments_data)

        # --- Stage 5: reconciliation (pure) ---
        creditable = verify.select_creditable_payments(
            payments_data,
            paid_statuses=self._settings.cloudpayments_paid_statuses(),
            now=_now(),
            freshness_hours=self._settings.cloudpayments_payment_freshness_hours,
        )
        if not creditable:
            return self._log_outcome(
                _ignored("no_creditable_payment"),
                transaction_id=transaction_id,
                user_id=resolved_user_id,
                resolved_via=resolved_via,
                verify_result="ok",
                credited_count=0,
                payment_statuses=statuses,
            )

        # --- Stage 6: credit each selected payment in its OWN transaction (idempotent) ---
        credited = 0
        for payment in creditable:
            if await self._apply_payment(payment, resolved_user_id):
                credited += 1

        outcome = (
            WebhookOutcome(result="applied")
            if credited >= 1
            else WebhookOutcome(result="duplicate")
        )
        return self._log_outcome(
            outcome,
            transaction_id=transaction_id,
            user_id=resolved_user_id,
            resolved_via=resolved_via,
            verify_result="ok",
            credited_count=credited,
            payment_statuses=statuses,
        )

    async def _resolve_user(self, x: uuid.UUID) -> tuple[uuid.UUID, str] | None:
        """Resolve the callback identifier ``X`` to our internal ``userId`` (ADR-053, two-step).

        broadapps sends a deviceId (not our userId) as ``AccountId``/``Data.user_id`` on the RU
        flow. First-match wins, deterministic (``users`` before ``auth_devices`` for compat):

        - (a) ``X`` in ``users`` -> ``X`` already IS our userId; ``resolved_via = "user_id"``.
        - (b) else ``X`` in ``auth_devices.device_id`` -> take the linked ``user_id`` (deviceId ->
          userId); ``resolved_via = "device_id"``. This is the RU-flow fix.
        - (c) else -> ``None`` (=> ``user_not_found``; we never provision users/devices, ADR-007).

        The deviceId->userId mapping is taken ONLY from our ``auth_devices`` (never from the
        callback body). ``X`` is an already-lower UUID; ``auth_devices.device_id`` is a lower UUID
        string, so ``str(x)`` matches. Both lookups run before the verification GET.
        """
        if await self._session.scalar(
            text("SELECT 1 FROM users WHERE id = :x"),
            {"x": str(x)},
        ):
            return x, "user_id"

        device_user_id = await self._session.scalar(
            text("SELECT user_id FROM auth_devices WHERE device_id = :x"),
            {"x": str(x)},
        )
        if device_user_id is not None:
            return uuid.UUID(str(device_user_id)), "device_id"

        return None

    async def _apply_payment(self, payment: CreditablePayment, user_id: uuid.UUID) -> bool:
        """Credit ONE verified payment in its own transaction; return True iff it was credited.

        Class from the authoritative ``payment_type`` (``one_time`` -> tokens, ``subscription`` ->
        subscription; anything else -> skip, WARNING). Amount from server-side maps keyed by
        ``product_code`` (anti-tamper; a token product missing / non-positive -> skip, WARNING).
        Then the event-delivery dedup INSERT keyed by ``payment_id``: an empty RETURNING means this
        payment was already credited -> skip (duplicate). Otherwise upsert any subscription, grant
        idempotently by ``cp-txn:{payment_id}``, audit, and COMMIT this payment's transaction.
        A DB failure rolls this payment back and propagates (=> 500 retriable); payments credited in
        earlier iterations stay committed (idempotency makes re-delivery safe).
        """
        # Classify by the authoritative payment_type (not the callback / product name).
        if payment.payment_type == "one_time":
            kind = parser.KIND_TOKENS
        elif payment.payment_type == "subscription":
            kind = parser.KIND_SUBSCRIPTION
        else:
            self._log_payment_skipped(payment, user_id, "unknown_payment_type")
            return False

        # Amount ONLY from server-side maps keyed by product_code (anti-tamper, ADR-054 §6).
        if kind == parser.KIND_TOKENS:
            credits = self._settings.token_products().get(payment.product_code)
            if credits is None or credits <= 0:
                self._log_payment_skipped(payment, user_id, "unknown_product")
                return False
            reason = "cloudpayments_tokens"
        else:
            credits = (
                self._settings.cloudpayments_product_tokens().get(payment.product_code)
                or self._settings.cloudpayments_subscription_tokens_grant
            )
            reason = "cloudpayments_subscription"

        try:
            # Event-delivery dedup keyed by broadapps payment_id (stored in transaction_id column).
            sanitized = {
                "paymentId": payment.payment_id,
                "productCode": payment.product_code,
                "paymentType": payment.payment_type,
                "status": payment.status,
                "kind": kind,
            }
            inserted = await self._session.scalar(
                text(
                    "INSERT INTO cloudpayments_webhook_events "
                    "(transaction_id, user_id, product_id, kind, payload) "
                    "VALUES (:txn, :uid, :product_id, :kind, CAST(:payload AS JSONB)) "
                    "ON CONFLICT (transaction_id) DO NOTHING "
                    "RETURNING transaction_id"
                ),
                {
                    "txn": payment.payment_id,
                    "uid": str(user_id),
                    "product_id": payment.product_code,
                    "kind": kind,
                    "payload": json.dumps(sanitized),
                },
            )
            if inserted is None:
                # Already credited on a previous delivery -> no mutations for this payment.
                await self._session.rollback()
                return False

            sub_status: str | None = None
            plan: str | None = None
            expires_at: datetime.datetime | None = None
            if kind == parser.KIND_SUBSCRIPTION:
                unit = parser.infer_interval_unit_from_code(payment.product_code)
                expires_at = parser._compute_expiry(_now(), unit, 1)
                plan = payment.product_code
                sub_status = "active"
                await self._upsert_subscription(user_id, plan, expires_at)

            await self._wallet.grant(
                user_id=user_id,
                amount=credits,
                idempotency_key=f"cp-txn:{payment.payment_id}",
                reason=reason,
                meta={
                    "paymentId": payment.payment_id,
                    "productCode": payment.product_code,
                    "kind": kind,
                },
            )
            await self._audit.record(
                AuditEvent(
                    user_id=user_id,
                    event_type=EVENT_CLOUDPAYMENTS_PAYMENT,
                    payload={
                        "transactionId": payment.payment_id,
                        "paymentId": payment.payment_id,
                        "productId": payment.product_code,
                        "kind": kind,
                        "semantics": kind,
                        "paymentType": payment.payment_type,
                        "status": sub_status,
                        "plan": plan,
                        "expiresAt": expires_at.isoformat() if expires_at is not None else None,
                        "creditsGranted": credits,
                        "paidAt": payment.paid_at.isoformat(),
                    },
                )
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

        # Optional per-payment visibility (the aggregate outcome is logged once in handle()).
        log_event(
            logger,
            logging.DEBUG,
            "cloudpayments_payment_credited",
            paymentId=payment.payment_id,
            productId=payment.product_code,
            kind=kind,
            userId=str(user_id),
            creditsGranted=credits,
        )
        return True

    def _log_payment_skipped(
        self, payment: CreditablePayment, user_id: uuid.UUID, reason: str
    ) -> None:
        """One WARNING per skipped payment (unknown product / payment_type) — ADR-054 §5.

        Separate from the single aggregate ``cloudpayments_webhook_outcome`` (invariant: exactly one
        outcome per callback). No amount / card data / secret is logged.
        """
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_payment_skipped",
            reason=reason,
            paymentId=payment.payment_id,
            productId=payment.product_code,
            paymentType=payment.payment_type,
            userId=str(user_id),
        )

    async def _upsert_subscription(
        self, user_id: uuid.UUID, plan: str, expires_at: datetime.datetime
    ) -> None:
        """Activate/extend the subscription in one statement (ADR-048 pattern, ADR-054 §7).

        ON CONFLICT (user_id) makes a concurrent first activation idempotent by PK. Parameterized;
        ``'active'`` casts to the subscription-status enum.
        """
        await self._session.execute(
            text(
                "INSERT INTO subscriptions (user_id, status, plan, expires_at, updated_at) "
                "VALUES (:uid, 'active', :plan, :expires_at, now()) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "status = 'active', plan = EXCLUDED.plan, "
                "expires_at = EXCLUDED.expires_at, updated_at = now()"
            ),
            {"uid": str(user_id), "plan": plan, "expires_at": expires_at},
        )
        await self._session.flush()
