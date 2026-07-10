"""Response schema for the Adapty subscription webhook (billing-adapty/02-api-contracts.md).

The endpoint reads the RAW request body (no Pydantic body model, ADR-029 §2) so there is no
request schema. This response model only documents the success envelope for OpenAPI / Swagger:
``{result, reason?, event_type?}``. ``result`` is one of ``ignored | duplicate | applied``.
"""

from __future__ import annotations

from pydantic import BaseModel


class AdaptyWebhookResponse(BaseModel):
    result: str
    reason: str | None = None
    event_type: str | None = None
