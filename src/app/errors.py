"""Domain technical errors mapped to HTTP codes (api-gateway/02-api-contracts.md, ADR-004).

Business blocks are NOT errors — they return 200 {status: blocked} (ADR-004).
These exceptions cover only technical failures (4xx/5xx).
"""

from __future__ import annotations


class AppError(Exception):
    """Base technical error. `code` is from the standard error enum."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.code
        super().__init__(self.message)


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class SubscriptionRequiredError(ForbiddenError):
    """Token purchase attempted without an active subscription (Q-015-1=B, ADR-015).

    403 with code=subscription_required: the value is reused from the ADR-004 enum but emitted
    as a 4xx error code (not a 200 blockReason) — token purchase is a top-up operation, not
    generation, so ADR-004 (blocked = 200) does not apply.
    """

    code = "subscription_required"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class SessionNotFoundError(NotFoundError):
    """sessionId passed to /wallet/consume does not exist in chat_sessions (wallet-ledger/02)."""

    code = "session_not_found"


class UserNotFoundError(NotFoundError):
    """userId targeted by an admin op does not exist; admin never creates users (ADR-009)."""

    code = "user_not_found"


class WorkspaceNotFoundError(NotFoundError):
    """workspaceProjectId bound at /chat/run session creation is foreign/missing (ADR-036 §3).

    404 with code=workspace_not_found: never reveal a foreign workspace's existence (isolation,
    workspaces/06-rbac). Distinct code so the client can map it to a workspace-specific UI.
    """

    code = "workspace_not_found"


class MessageNotFoundError(NotFoundError):
    """editMessageStepId in /chat/run does not resolve to a user-step of the session (ADR-040 §1).

    404 with code=message_not_found (chat-orchestrator/02-api-contracts.md, anchor
    editmessagestepid-adr-040): the message-step to edit was not found — either the session is
    foreign/missing/expired (resume not performed, no turn to edit), or there is no `role='user'`
    step with that message_step_id (anchor is matched strictly by role='user', ADR-040 §4в).
    Distinct `code` per the contract's machine-readable value, mirroring the *_not_found family
    (workspace/session/user). The ADR §3 normative note phrases this as
    ``raise NotFoundError("message_not_found")``; the contract anchor specifies the wire `code`
    `message_not_found`, so a dedicated subclass with that `code` satisfies both (the error handler
    serializes `exc.code`, not `exc.message`).
    """

    code = "message_not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class InsufficientCreditsError(ConflictError):
    """Balance changed below required amount after policy allow (wallet-ledger/02)."""

    code = "insufficient_credits"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


class ValidationFailedError(AppError):
    status_code = 422
    code = "validation_error"


class UnsupportedDialogModeError(ValidationFailedError):
    """dialogMode is unknown OR provider-gated to OpenAI on a non-OpenAI instance (ADR-055 §4).

    422 with a DEDICATED machine-readable code ``unsupported_dialog_mode`` (chat-orchestrator/02,
    anchor dialogmode-adr-055): the client builds the Modes menu from the instance's capabilities,
    so requesting a mode the instance does not support is a bad request, NOT a business block
    (ADR-004) — hence 422, not ``blocked``, and it is NOT in the ``blockReason`` enum. Mirrors the
    ``unsupported_model`` gate (ADR-034 §3); unlike that gate (which reuses the generic
    ``validation_error`` code), ADR-055 requires a NAMED code, so this dedicated subclass overrides
    ``code`` (the error handler serializes ``exc.code``, not ``exc.message``).
    """

    code = "unsupported_dialog_mode"


class UnsupportedModelError(ValidationFailedError):
    """model is not in the active instance's allowlist (ADR-034 §3, ревизия).

    422 with a DEDICATED machine-readable code ``unsupported_model`` (chat-orchestrator/02,
    API-REFERENCE.md): the client picks a model from ``GET /v1/models``, so requesting one the
    instance does not offer is a bad request, NOT a business block (ADR-004) — hence 422, not
    ``blocked``, and it is NOT in the ``blockReason`` enum. Unified UPWARD with
    ``UnsupportedDialogModeError`` (ADR-034 §3 revision): the model gate previously reused the
    generic ``validation_error`` code; it now emits a named code, symmetric to
    ``unsupported_dialog_mode``. Dedicated subclass overrides ``code`` (the error handler
    serializes ``exc.code``, not ``exc.message``); the HTTP 422 status is inherited unchanged.
    """

    code = "unsupported_model"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


class UpstreamError(AppError):
    status_code = 502
    code = "upstream_error"


class ServiceUnavailableError(AppError):
    """A required dependency/feature is not configured (e.g. auth issuer has no private key).

    503 service_unavailable: used by the embedded auth-issuer endpoints when no private signing
    key is configured (ADR-018 §7); verify-only mode keeps working on the public key.
    """

    status_code = 503
    code = "service_unavailable"


class CloudPaymentsWebhookMisconfiguredError(ServiceUnavailableError):
    """The RU webhook cannot verify payments because ``CLOUDPAYMENTS_API_TOKEN`` is unset (ADR-054).

    500 with code=cloudpayments_webhook_misconfigured (billing-cloudpayments/02-api-contracts.md).
    Under ADR-054 the instance activation gate moved from ``CLOUDPAYMENTS_WEBHOOK_TOKEN`` (legacy,
    no longer gates) to ``CLOUDPAYMENTS_API_TOKEN`` (the outgoing Bearer used for verification):
    without it no payment can be confirmed, so ``handle()`` raises this BEFORE any parsing and the
    aggregator retries until the operator sets the token. So the webhook credits only where the API
    token is set (avelyra). Overrides ``status_code`` to 500 (not the 503 of the base) so the
    aggregator treats it as a transient server fault to retry.
    """

    status_code = 500
    code = "cloudpayments_webhook_misconfigured"


class CloudPaymentsVerificationUnavailableError(AppError):
    """broadapps payment-verification GET failed transiently — credit deferred, retriable (ADR-054).

    500 with code=cloudpayments_verification_unavailable: a timeout / connect error / 5xx / a
    malformed (non-JSON or missing ``data``) response from ``GET /users/{deviceId}/payments`` must
    not silently drop a real payment. The service raises this so the whole callback returns 500 and
    the aggregator re-delivers later (idempotency by broadapps ``payment_id`` keeps reprocessing
    safe). A broadapps ``404`` is NOT this error — it means "no payments" (permanent) and yields
    ``no_creditable_payment`` (200). The upstream body/status/token are never proxied outward.
    """

    status_code = 500
    code = "cloudpayments_verification_unavailable"


class CloudPaymentsCheckoutNotConfiguredError(ServiceUnavailableError):
    """RU checkout is not configured on this instance (ADR-051 §5).

    503 with code=cloudpayments_checkout_not_configured: CLOUDPAYMENTS_APP_ID /
    CLOUDPAYMENTS_API_TOKEN are unset (the endpoint is active only where the operator sets both,
    i.e. avelyra). Distinct
    machine-readable code so the iOS client can tell "feature not available on this instance" apart
    from a broadapps outage (502) — consistent with other not-configured user-facing endpoints
    (Apple sign-in / embedded auth-issuer return 503).
    """

    code = "cloudpayments_checkout_not_configured"
