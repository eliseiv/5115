"""AdaptyWebhookService: defensive parse -> dedup -> upsert subscription -> grant -> audit.

Implements ADR-029 §2-§7 / billing-adapty/03-architecture.md. ``handle(raw)`` never raises on a
malformed / unrecognised payload — it returns an ``ignored`` / ``duplicate`` / ``applied`` outcome
(the router maps every one of these to HTTP 200). It DOES raise on a real internal failure (e.g.
the DB is unavailable): the caller's session_scope then rolls the whole transaction back and the
router surfaces 500, which Adapty retries — on retry ``event_id`` is free again (the INSERT was
rolled back) so reprocessing is clean (and ``grant`` is additionally idempotent by key).

Two independent idempotency layers (ADR-047 §C): event-delivery dedup lives in the single statement
``INSERT adapty_webhook_events ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id`` (event_id
= ``profile_event_id``); empty RETURNING => duplicate => no mutations. Credit-grant idempotency is
separate — keyed by ``adapty-txn:{transaction_id}`` so one billing period grants exactly once even
across the several granting-events Adapty emits per purchase. The semantics-based dispatch
(``classify_event`` -> GRANTING upserts+grants / EXPIRING expires / NOOP keeps access) + audit run
in the SAME transaction as the dedup INSERT.
"""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import EVENT_ADAPTY_SUBSCRIPTION, AuditEvent, AuditService
from app.billing_adapty import parser
from app.billing_adapty.parser import ParsedEvent
from app.config import Settings
from app.models import Subscription
from app.observability.logging import log_event
from app.wallet.service import WalletService

logger = logging.getLogger(__name__)  # == "app.billing_adapty.service"


@dataclass(frozen=True)
class WebhookOutcome:
    """Result of handling one webhook call. The router maps it to an HTTP-200 JSON envelope."""

    result: str  # "ignored" | "duplicate" | "applied"
    reason: str | None = None
    event_type: str | None = None


def _ignored(reason: str | None = None, event_type: str | None = None) -> WebhookOutcome:
    return WebhookOutcome(result="ignored", reason=reason, event_type=event_type)


def _level_for(result: str, reason: str | None) -> int:
    """Map a webhook outcome to a log level (ADR-046 §Таблица уровней / 08-observability.md).

    WARNING = "Adapty sent something event-like but the credit could not be delivered"
    (the incident class): ``user_not_found`` / ``missing_customer_user_id`` and the unknown
    ``event_type`` echo (the only ``ignored`` outcome carrying ``reason is None``). ``applied`` /
    ``duplicate`` and the parse-shape garbage (``invalid_json`` / ``not_an_object`` /
    ``missing_event_id``) are INFO; a fully empty body is a low-signal connectivity probe -> DEBUG.
    """
    if result in ("applied", "duplicate"):
        return logging.INFO
    # result == "ignored"
    if reason in ("user_not_found", "missing_customer_user_id"):
        return logging.WARNING
    if reason is None:
        # The only ignored outcome with reason=None is the unknown-event_type echo.
        return logging.WARNING
    if reason == "empty_body":
        return logging.DEBUG
    # invalid_json | not_an_object | missing_event_id
    return logging.INFO


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class AdaptyWebhookService:
    def __init__(
        self,
        session: AsyncSession,
        wallet: WalletService,
        audit: AuditService,
        settings: Settings,
    ) -> None:
        self._session = session
        self._wallet = wallet
        self._audit = audit
        self._settings = settings

    def _log_outcome(
        self,
        outcome: WebhookOutcome,
        *,
        event_type: str | None = None,
        event_id: str | None = None,
        customer_user_id: uuid.UUID | None = None,
    ) -> WebhookOutcome:
        """Emit exactly one structured ``adapty_webhook_outcome`` record and return ``outcome``.

        Returns the same outcome so it can wrap an existing ``return`` without changing control
        flow (ADR-046 / 08-observability.md). Only the fixed allowlist is logged (result / reason /
        eventType / eventId / customerUserId); the raw payload and the bearer secret are never
        logged. ``customer_user_id`` is our internal UUID -> ``str(...)`` so ``json.dumps`` in the
        ``JsonFormatter`` cannot fail; ``None`` fields are dropped from the JSON (= "not parsed").
        """
        level = _level_for(outcome.result, outcome.reason)
        log_event(
            logger,
            level,
            "adapty_webhook_outcome",
            result=outcome.result,
            reason=outcome.reason,
            eventType=event_type,
            eventId=event_id,
            customerUserId=str(customer_user_id) if customer_user_id is not None else None,
        )
        return outcome

    async def handle(self, raw: bytes) -> WebhookOutcome:
        """Process one raw webhook body. Always returns a 200-mappable outcome unless the DB fails.

        Pre-transaction validation (empty / not-JSON / not-object / missing id / missing user /
        user-not-found / unknown type) yields ``ignored`` with no DB writes. A recognised event is
        applied inside the caller's transaction; any real DB failure propagates (=> rollback/500).
        """
        # --- Stage 1: body shape (no DB) ---
        if not raw:
            return self._log_outcome(_ignored("empty_body"))
        try:
            body: Any = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return self._log_outcome(_ignored("invalid_json"))
        if not isinstance(body, dict):
            return self._log_outcome(_ignored("not_an_object"))

        # --- Stage 2: defensive field parsing (no DB) ---
        event_id = parser.parse_event_id(body)
        if event_id is None:
            return self._log_outcome(_ignored("missing_event_id"))
        # ADR-047 (synergy with ADR-046): parse event_type BEFORE the customer_user_id check (a
        # pure, DB-free op) so the missing_customer_user_id WARNING carries the event type —
        # operators see "trial_started arrived but no customer_user_id" instead of a faceless
        # reason. "" -> None in the log allowlist (no event_type parsed).
        event_type = parser.parse_event_type(body)
        customer_user_id = parser.parse_customer_user_id(body)
        if customer_user_id is None:
            # Absent or non-UUID customer_user_id is equivalent to "user not found" per contract,
            # but the contract distinguishes the reason: a missing/non-UUID id is reported as
            # missing_customer_user_id (02-api-contracts.md), whereas a well-formed UUID with no
            # matching users row is user_not_found. Until iOS calls Adapty.identify the real payload
            # only carries Adapty's profile_id -> this is the expected (correct) reason.
            return self._log_outcome(
                _ignored("missing_customer_user_id"),
                event_type=event_type or None,
                event_id=event_id,
            )

        # --- Stage 3: user existence (DB read; we never provision users here) ---
        if not await self._user_exists(customer_user_id):
            return self._log_outcome(
                _ignored("user_not_found"),
                event_type=event_type,
                event_id=event_id,
                customer_user_id=customer_user_id,
            )

        # --- Stage 4: event-type dispatch ---
        if event_type not in parser.KNOWN_EVENTS:
            # Echo the (normalised) event_type so operators can see what arrived. No audit, no
            # mutation: an unknown type is "no event happened" (architect-reviewer minor).
            return self._log_outcome(
                _ignored(event_type=event_type),
                event_type=event_type,
                event_id=event_id,
                customer_user_id=customer_user_id,
            )

        parsed = ParsedEvent(
            event_id=event_id,
            event_type=event_type,
            customer_user_id=customer_user_id,
            vendor_product_id=parser.parse_vendor_product_id(body),
            expires_at=parser.parse_expires_at(body),
            transaction_id=parser.parse_transaction_id(body),
            original_transaction_id=parser.parse_original_transaction_id(body),
            is_active=parser.parse_is_active(body),
            access_level_id=parser.parse_access_level_id(body),
            will_renew=parser.parse_will_renew(body),
        )
        return await self._apply(parsed, body)

    async def _user_exists(self, user_id: uuid.UUID) -> bool:
        exists = await self._session.scalar(
            text("SELECT 1 FROM users WHERE id = :uid"),
            {"uid": str(user_id)},
        )
        return exists is not None

    async def _apply(self, event: ParsedEvent, body: dict[str, Any]) -> WebhookOutcome:
        """Apply a recognised event inside the caller's single transaction (ADR-029 §6 / ADR-047).

        The INSERT ... ON CONFLICT DO NOTHING RETURNING is the sole event-delivery dedup point: an
        empty RETURNING means a concurrent/previous delivery already recorded this event_id ->
        duplicate, no mutations. Otherwise the semantics are resolved by ``classify_event``
        (GRANTING / EXPIRING / NOOP) and dispatched: GRANTING upserts an active subscription and
        grants credits (idempotent per billing period by ``adapty-txn:{txn}``); EXPIRING marks the
        subscription expired (credits untouched); NOOP (auto-renew turned off, access kept) touches
        neither subscription nor credits but is still recorded + audited. The audit row runs in this
        transaction, committed on success.
        """
        inserted = await self._session.scalar(
            text(
                "INSERT INTO adapty_webhook_events (event_id, user_id, event_type, payload) "
                "VALUES (:event_id, :uid, :event_type, CAST(:payload AS JSONB)) "
                "ON CONFLICT (event_id) DO NOTHING "
                "RETURNING event_id"
            ),
            {
                "event_id": event.event_id,
                "uid": str(event.customer_user_id),
                "event_type": event.event_type,
                "payload": json.dumps(body),
            },
        )
        if inserted is None:
            # Duplicate event_id: no mutations (ADR-029 §6, architect-reviewer minor).
            return self._log_outcome(
                WebhookOutcome(result="duplicate"),
                event_type=event.event_type,
                event_id=event.event_id,
                customer_user_id=event.customer_user_id,
            )

        semantics = parser.classify_event(event)
        # transaction_id is unique per billing period (primary grant idem key);
        # original_transaction_id is stable across the chain (fallback); event_id is last resort.
        txn = event.transaction_id or event.original_transaction_id or event.event_id

        if semantics == parser.SEM_NOOP:
            # Auto-renew cancellation: access is kept until period end -> touch neither subscription
            # nor credits. Echo the current subscription state (if any) into the audit row.
            status, plan = await self._read_subscription(event.customer_user_id)
        else:
            status, plan = await self._upsert_subscription(event, semantics)
            if semantics == parser.SEM_GRANTING:
                await self._grant(event, txn)

        await self._audit.record(
            AuditEvent(
                user_id=event.customer_user_id,
                event_type=EVENT_ADAPTY_SUBSCRIPTION,
                payload={
                    "adaptyEventId": event.event_id,
                    "eventType": event.event_type,
                    "semantics": semantics,
                    "status": status,
                    "plan": plan,
                    "expiresAt": event.expires_at.isoformat() if event.expires_at else None,
                    "transactionId": txn,
                    "willRenew": event.will_renew,
                    "customerId": str(event.customer_user_id),
                },
            )
        )
        return self._log_outcome(
            WebhookOutcome(result="applied"),
            event_type=event.event_type,
            event_id=event.event_id,
            customer_user_id=event.customer_user_id,
        )

    async def _read_subscription(self, user_id: uuid.UUID) -> tuple[str | None, str | None]:
        """Read the current (status, plan) without mutating — used for the NOOP audit row."""
        row = await self._session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        if row is None:
            return None, None
        return row.status, row.plan

    async def _upsert_subscription(
        self, event: ParsedEvent, semantics: str
    ) -> tuple[str, str | None]:
        """Upsert subscriptions per the resolved semantics (ADR-047 §B). Returns (status, plan).

        GRANTING -> active, plan=vendor_product_id, expires_at (if present).
        EXPIRING -> expired; plan and expires_at are left unchanged. NOOP never reaches here.
        """
        row = await self._session.scalar(
            select(Subscription).where(Subscription.user_id == event.customer_user_id)
        )
        if semantics == parser.SEM_GRANTING:
            status = "active"
            plan = event.vendor_product_id
            if row is None:
                row = Subscription(
                    user_id=event.customer_user_id,
                    status=status,
                    plan=plan,
                    expires_at=event.expires_at,
                )
                self._session.add(row)
            else:
                row.status = status
                row.plan = plan
                row.expires_at = event.expires_at
                row.updated_at = _now()
        else:
            # EXPIRING: mark expired, do not touch plan / expires_at / credits.
            status = "expired"
            if row is None:
                row = Subscription(
                    user_id=event.customer_user_id,
                    status=status,
                    plan=None,
                    expires_at=None,
                )
                self._session.add(row)
                plan = None
            else:
                row.status = status
                row.updated_at = _now()
                plan = row.plan
        await self._session.flush()
        return status, plan

    async def _grant(self, event: ParsedEvent, txn: str) -> None:
        """Grant credits by product tier, idempotent by ``adapty-txn:{txn}`` (ADR-047 §C).

        ``txn`` is the per-period transaction id (resolved in ``_apply``): one purchase period maps
        to exactly one grant no matter how many granting-events it emits, while each renewal (new
        transaction_id) grants afresh. Only granting-events call this.
        """
        tier = self._tier_for(event.vendor_product_id)
        await self._wallet.grant(
            user_id=event.customer_user_id,
            amount=tier,
            idempotency_key=f"adapty-txn:{txn}",
            reason="adapty_subscription",
            meta={
                "transactionId": txn,
                "eventType": event.event_type,
                "vendorProductId": event.vendor_product_id,
            },
        )

    def _tier_for(self, vendor_product_id: str | None) -> int:
        """tokens = product-tier map[vendor_product_id] or the fixed fallback grant (ADR-029 §5)."""
        mapped = self._settings.adapty_product_tokens().get(vendor_product_id or "")
        return mapped or self._settings.adapty_subscription_tokens_grant
