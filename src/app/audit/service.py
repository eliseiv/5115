"""Append-only audit logging (audit/02,03; AC-7).

Only INSERT; never UPDATE/DELETE (TD-001 for DB-level enforcement). Payload is redacted
before insert (05-security.md). Critical events (billing_debit, tool_mutation) are written
within the same DB transaction as the action so there is never "action without audit".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog
from app.observability.context import get_request_id
from app.observability.redaction import assert_no_secrets

# eventType catalog (audit/02-api-contracts.md)
EVENT_TOOL_MUTATION = "tool_mutation"
EVENT_BILLING_DEBIT = "billing_debit"
EVENT_BILLING_CREDIT = "billing_credit"
EVENT_POLICY_DECISION = "policy_decision"
EVENT_BYOK_CHANGE = "byok_change"
EVENT_SUBSCRIPTION_CHANGE = "subscription_change"
EVENT_ADAPTY_SUBSCRIPTION = "adapty_subscription"
EVENT_CLOUDPAYMENTS_PAYMENT = "cloudpayments_payment"
EVENT_CHAT_STEP = "chat_step"
EVENT_TOOL_CALL_INITIATED = "tool_call_initiated"
EVENT_TOOL_CALL_COMPLETED = "tool_call_completed"
EVENT_ADMIN_GRANT = "admin_grant"
EVENT_ADMIN_SUBSCRIPTION_GRANT = "admin_subscription_grant"


@dataclass(frozen=True)
class AuditEvent:
    user_id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    session_id: uuid.UUID | None = None


class AuditService:
    """Records audit events. Uses the caller's session so it joins the same transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, event: AuditEvent) -> None:
        payload = assert_no_secrets({**event.payload, "requestId": get_request_id()})
        row = AuditLog(
            user_id=event.user_id,
            session_id=event.session_id,
            event_type=event.event_type,
            payload=payload,
        )
        self._session.add(row)
        await self._session.flush()
