"""Application configuration from environment (pydantic-settings).

All secrets and tunables come from env / secret manager (05-security.md, 07-deployment.md).
No magic numbers in business code: limits and grant size are config-driven (ADR-006).
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Default payment-freshness window (hours) for CloudPayments verification/reconciliation (ADR-054
# §Окно свежести). A non-positive CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS falls back to this.
_CLOUDPAYMENTS_DEFAULT_FRESHNESS_HOURS = 72

# Valid OpenAI reasoning effort / web-search context-size levels (ADR-055/ADR-059). A malformed
# env value degrades to "medium" via the resolved_* helpers (graceful config, never a crash).
_REASONING_EFFORT_VALUES: frozenset[str] = frozenset({"low", "medium", "high"})
_SEARCH_CONTEXT_SIZE_VALUES: frozenset[str] = frozenset({"low", "medium", "high"})

# Image generation allowlists (ADR-058, OpenAI gpt-image-1). Public (exported) so the tool-args
# validator (app.chat.tools.ImageGenerateArgs) checks a model-supplied size/quality against the
# SAME source of truth the resolved_* helpers use for the instance default. A malformed DEFAULT env
# degrades to a safe value via resolved_image_* (graceful config, never a crash); a malformed
# model-supplied value is a tool-args validation error (like any other tool).
IMAGE_SIZE_VALUES: frozenset[str] = frozenset({"1024x1024", "1024x1536", "1536x1024", "auto"})
IMAGE_QUALITY_VALUES: frozenset[str] = frozenset({"low", "medium", "high", "auto"})
IMAGE_OUTPUT_FORMAT_VALUES: frozenset[str] = frozenset({"png", "jpeg", "webp"})
_IMAGE_DEFAULT_SIZE = "1024x1024"
_IMAGE_DEFAULT_QUALITY = "high"
_IMAGE_DEFAULT_OUTPUT_FORMAT = "png"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Storage ---
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/claude_ios",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # --- LLM provider selection (ADR-033) ---
    # One provider per instance. Default "anthropic" → existing instances (claude-ios/avelyra)
    # are unchanged; "openai" activates the OpenAI Chat Completions path. The OpenAI clone is a
    # separate instance with LLM_PROVIDER=openai + OPENAI_* (07-deployment.md §Мульти-инстанс).
    llm_provider: str = Field(default="anthropic", alias="LLM_PROVIDER")

    # --- Model allowlist per provider (ADR-034) ---
    # JSON object {model-id: displayName} of the models a user may pick on this instance. Parsed
    # by allowed_models() with the SAME shape rules as token_products() (str→non-empty-str only).
    # Default "{}" → empty allowlist → backward-compatible fallback to the single instance default
    # model (allowed_models()). Per-provider: only the active provider's raw is read. Not secrets.
    anthropic_models_raw: str = Field(default="{}", alias="ANTHROPIC_MODELS")
    openai_models_raw: str = Field(default="{}", alias="OPENAI_MODELS")

    # --- OpenAI (ADR-033; used only when LLM_PROVIDER=openai) ---
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o", alias="OPENAI_MODEL")
    # Output budget per call (parity with ANTHROPIC_MAX_TOKENS=16000).
    openai_max_tokens: int = Field(default=16000, alias="OPENAI_MAX_TOKENS")
    openai_timeout_seconds: float = Field(default=120.0, alias="OPENAI_TIMEOUT_SECONDS")
    openai_max_retries: int = Field(default=2, alias="OPENAI_MAX_RETRIES")
    # BYOK active model reported when keyStatus=valid on an OpenAI instance (ADR-016/ADR-033 §7).
    openai_byok_default_model: str = Field(default="gpt-4o", alias="OPENAI_BYOK_DEFAULT_MODEL")

    # --- Dialog modes (ADR-055/ADR-059; effective only when LLM_PROVIDER=openai) ---
    # Deep Thinking: the reasoning model is FORCED by dialogMode=deep_thinking, overriding the
    # user's chat.model and the stale-model guard (incl. BYOK — ADR-059 §4). Defaults match
    # .env(.prod).example so the code and the ops templates never diverge.
    deep_thinking_model: str = Field(default="o4-mini", alias="DEEP_THINKING_MODEL")
    # Reasoning effort low|medium|high; a malformed value degrades to the safe default via
    # resolved_deep_thinking_effort() (never read the raw field directly).
    deep_thinking_effort: str = Field(default="medium", alias="DEEP_THINKING_EFFORT")
    # Per-call timeout for deep_thinking (reasoning is slower than a plain turn); applied by the
    # OpenAI client only on the deep_thinking path.
    deep_thinking_timeout_seconds: float = Field(
        default=180.0, alias="DEEP_THINKING_TIMEOUT_SECONDS"
    )
    # Search: search_context_size of the built-in web_search tool; low|medium|high; a malformed
    # value degrades to the safe default via resolved_search_context_size().
    openai_search_context_size: str = Field(default="medium", alias="OPENAI_SEARCH_CONTEXT_SIZE")

    # --- Image generation (ADR-058; OpenAI gpt-image-1 via a SEPARATE images.generate client) ---
    # Per-image debit charged ON TOP of the 1-credit turn (total = 1 + N*IMAGE_CREDITS_COST),
    # credits-branch ONLY (byok/trial images are free). Insufficient balance for an image →
    # status=blocked, blockReason=image_credits_empty (bytes not saved, ADR-058 §5).
    image_credits_cost: int = Field(default=5, alias="IMAGE_CREDITS_COST")
    image_model: str = Field(default="gpt-image-1", alias="IMAGE_MODEL")
    # Instance default size/quality/output-format when the model omits them. A malformed env value
    # degrades to the safe default via resolved_image_* (never read the raw field directly).
    image_default_size: str = Field(default=_IMAGE_DEFAULT_SIZE, alias="IMAGE_DEFAULT_SIZE")
    image_default_quality: str = Field(
        default=_IMAGE_DEFAULT_QUALITY, alias="IMAGE_DEFAULT_QUALITY"
    )
    image_output_format: str = Field(
        default=_IMAGE_DEFAULT_OUTPUT_FORMAT, alias="IMAGE_OUTPUT_FORMAT"
    )
    # Own timeout/retries for the image client (image generation is slower than a text turn).
    image_timeout_seconds: float = Field(default=120.0, alias="IMAGE_TIMEOUT_SECONDS")
    image_max_retries: int = Field(default=2, alias="IMAGE_MAX_RETRIES")
    # Decoded-byte ceiling for one generated image (defensive memory/storage bound); a larger
    # result → ImageGenerationError (the turn degrades to a tool_result error, ADR-058 §3).
    image_max_bytes: int = Field(default=16 * 1024 * 1024, alias="IMAGE_MAX_BYTES")
    # TTL for images of a TEMPORARY chat (ADR-056/ADR-058 §6): expires_at = created_at + this many
    # seconds (default 24h). A normal chat stores expires_at = NULL (never expires).
    temporary_image_ttl_seconds: int = Field(default=86400, alias="TEMPORARY_IMAGE_TTL_SECONDS")
    # Opportunistic sweep of expired image rows (ADR-058 §6, best-effort — TD-036). Batch size per
    # sweep and the min interval between sweeps (Redis throttle+lock, one worker per interval).
    image_sweep_batch_size: int = Field(default=100, alias="IMAGE_SWEEP_BATCH_SIZE")
    image_sweep_min_interval_seconds: int = Field(
        default=300, alias="IMAGE_SWEEP_MIN_INTERVAL_SECONDS"
    )

    # --- Anthropic ---
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-5", alias="ANTHROPIC_MODEL")
    # ADR-025: output budget per call. Raised 4096→16000 so code/file generation (several
    # files.write with full content) is not truncated by max_tokens. Stays non-streaming; 16000
    # is below the SDK non-streaming guard. Per-instance in .env (applied to every deploy instance).
    anthropic_max_tokens: int = Field(default=16000, alias="ANTHROPIC_MAX_TOKENS")
    # ADR-025: raised 60→120 to avoid a false 502 timeout on a long non-streaming generation at
    # max_tokens=16000. Configurable; still well below the SDK non-streaming guard.
    anthropic_timeout_seconds: float = Field(default=120.0, alias="ANTHROPIC_TIMEOUT_SECONDS")
    anthropic_max_retries: int = Field(default=2, alias="ANTHROPIC_MAX_RETRIES")
    # ADR-016: active model reported in BYOK responses when keyStatus=valid. Defaults to a
    # current Claude model; configurable via env. Not a secret (model name).
    byok_default_model: str = Field(default="claude-sonnet-4-6", alias="BYOK_DEFAULT_MODEL")

    # --- JWT (RS256, 05-security.md, Q-005-1 default own issuer) ---
    jwt_jwks_url: str = Field(default="", alias="JWT_JWKS_URL")
    jwt_issuer: str = Field(default="", alias="JWT_ISSUER")
    jwt_audience: str = Field(default="", alias="JWT_AUDIENCE")
    # Optional static public key (PEM) fallback when JWKS endpoint is not configured.
    jwt_public_key: str = Field(default="", alias="JWT_PUBLIC_KEY")
    jwks_cache_ttl_seconds: int = Field(default=300, alias="JWT_JWKS_CACHE_TTL")

    # --- Embedded auth-issuer (ADR-018, modules/auth) ---
    # Private signing key (RS256). SECRET: never in repo/image/logs (redaction). Provided as a
    # PEM file path (preferred in prod: mounted secret) or as a PEM string with \n-escaping in
    # env. Path takes priority. Absent => issuer endpoints return 503 (verify-only still works).
    jwt_private_key: str = Field(default="", alias="JWT_PRIVATE_KEY")
    jwt_private_key_path: str = Field(default="", alias="JWT_PRIVATE_KEY_PATH")
    # Public key file path (alongside the existing PEM-string JWT_PUBLIC_KEY; path takes priority).
    jwt_public_key_path: str = Field(default="", alias="JWT_PUBLIC_KEY_PATH")
    # Key id placed in the JWT header / JWKS (key rotation groundwork, not MVP).
    jwt_kid: str = Field(default="", alias="JWT_KID")
    # Access-token TTL 1h, refresh-token TTL 30d (ADR-018 §5).
    auth_access_ttl_seconds: int = Field(default=3600, alias="AUTH_ACCESS_TTL_SECONDS")
    auth_refresh_ttl_seconds: int = Field(default=2592000, alias="AUTH_REFRESH_TTL_SECONDS")
    # Per-IP rate limit on /v1/auth/* (anti-abuse mass registration).
    auth_rate_limit_per_ip: int = Field(default=10, alias="AUTH_RATE_LIMIT_PER_IP")
    # Toggle GET /v1/auth/jwks (public, non-secret). Default true.
    auth_jwks_enabled: bool = Field(default=True, alias="AUTH_JWKS_ENABLED")

    # --- KMS (envelope encryption, ADR-003, Q-002-1) ---
    kms_key_id: str = Field(default="", alias="KMS_KEY_ID")
    # Local fallback master key (base64, 32 bytes) for non-cloud envs; prod uses real KMS.
    kms_local_master_key: str = Field(default="", alias="KMS_LOCAL_MASTER_KEY")

    # --- App Store (Q-007-1) ---
    appstore_environment: str = Field(default="sandbox", alias="APPSTORE_ENVIRONMENT")
    appstore_bundle_id: str = Field(default="", alias="APPSTORE_BUNDLE_ID")
    appstore_root_cert_dir: str = Field(default="", alias="APPSTORE_ROOT_CERT_DIR")

    # --- Sign in with Apple (ADR-043, modules/auth Phase 6) ---
    # Apple OIDC identity-token verification for POST /v1/auth/apple. Native Sign in with Apple
    # only (aud = app bundle id); Services ID / web-flow is out of scope (Q-043-1). Values are
    # env (not secrets except APPLE_TEST_SECRET) and per-instance, like APPSTORE_BUNDLE_ID.
    apple_oidc_issuer: str = Field(default="https://appleid.apple.com", alias="APPLE_OIDC_ISSUER")
    apple_jwks_url: str = Field(
        default="https://appleid.apple.com/auth/keys", alias="APPLE_JWKS_URL"
    )
    # Expected `aud` = app bundle id. Empty => fall back to APPSTORE_BUNDLE_ID
    # (apple_audience_resolved()); both empty => Apple sign-in "not configured" => 503.
    apple_audience: str = Field(default="", alias="APPLE_AUDIENCE")
    # test-mode (ADR-043 §2): env-gated HS256 identity tokens for hermetic tests (no Apple infra).
    # Default false => prod fail-closed RS256 verification is unchanged. Active ONLY when
    # apple_test_mode is true AND apple_test_secret is non-empty; HS256 outside test-mode => 401
    # (no alg-confusion). The secret is redaction-allowlisted (`*secret*`) and never logged.
    apple_test_mode: bool = Field(default=False, alias="APPLE_TEST_MODE")
    apple_test_secret: str = Field(default="", alias="APPLE_TEST_SECRET")

    # --- StoreKit test-mode (TD-007, 09-e2e-testing.md §2; test/CI only) ---
    # Env-gated HS256 test transactions for e2e (no Apple infra). Default false => prod
    # fail-closed real JWS verification is unchanged. Active ONLY when storekit_test_mode is
    # true AND storekit_test_secret is non-empty. The secret is redaction-allowlisted and
    # never logged (05-security.md).
    storekit_test_mode: bool = Field(default=False, alias="STOREKIT_TEST_MODE")
    storekit_test_secret: str = Field(default="", alias="STOREKIT_TEST_SECRET")

    # --- Billing (ADR-006) ---
    subscription_credits_per_period: int = Field(
        default=1000, alias="SUBSCRIPTION_CREDITS_PER_PERIOD"
    )

    # --- Adapty subscription webhook (ADR-029, billing-adapty/07) ---
    # Isolated static bearer secret for POST /v1/billing/adapty/webhook. Set by the operator in
    # the Adapty UI; compared constant-time (hmac.compare_digest). Separate from JWT / admin /
    # KMS / preview secrets and per-instance (ADR-017). Empty (default) => the endpoint returns
    # 500 (misconfiguration); a blank secret never authenticates any presented token.
    adapty_webhook_secret: str = Field(default="", alias="ADAPTY_WEBHOOK_SECRET")
    # JSON object vendor_product_id -> tokens. Source of truth for the per-product grant tier on
    # subscription_started/renewed. Parsed by adapty_product_tokens() (same shape as
    # token_products()). Malformed/non-object => {} => every product falls back to the fixed grant.
    adapty_product_tokens_raw: str = Field(default="{}", alias="ADAPTY_PRODUCT_TOKENS")
    # Fixed fallback grant (tokens) used when vendor_product_id is absent from the tier map.
    # Isolated from SUBSCRIPTION_CREDITS_PER_PERIOD so the Adapty path is calibrated independently
    # (ADR-029 §5); defaults coincide (1000) for predictability.
    adapty_subscription_tokens_grant: int = Field(
        default=1000, alias="ADAPTY_SUBSCRIPTION_TOKENS_GRANT"
    )

    # --- CloudPayments (broadapps/YooKassa) RU webhook (ADR-050, billing-cloudpayments/07) ---
    # Isolated static bearer secret for POST /v1/billing/cloudpayments/webhook (on avelyra = the
    # broadapps app API key). Compared constant-time (hmac.compare_digest); separate from JWT /
    # admin / Adapty / KMS / preview secrets and per-instance (ADR-017). Empty (default) => the
    # endpoint returns 500 (misconfiguration) so it is active only where the secret is set.
    cloudpayments_webhook_token: str = Field(default="", alias="CLOUDPAYMENTS_WEBHOOK_TOKEN")
    # JSON object productId -> tokens: per-tier credits granted on a subscription payment. Parsed
    # by cloudpayments_product_tokens() (same shape as token_products()). Malformed/non-object =>
    # {} => every subscription falls back to the fixed grant below.
    cloudpayments_product_tokens_raw: str = Field(
        default="{}", alias="CLOUDPAYMENTS_PRODUCT_TOKENS"
    )
    # Fixed fallback grant (tokens) for a subscription product absent from the per-tier map above.
    # Isolated from SUBSCRIPTION_CREDITS_PER_PERIOD / the Adapty path so the RU path is calibrated
    # independently (ADR-050 §3a).
    cloudpayments_subscription_tokens_grant: int = Field(
        default=1000, alias="CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT"
    )
    # --- CloudPayments webhook payment verification (ADR-054) ---
    # broadapps sends the callback WITHOUT auth/signature, so it is only a TRIGGER: the endpoint
    # verifies the payment via the broadapps API (GET /users/{deviceId}/payments) with our
    # CLOUDPAYMENTS_API_TOKEN before crediting. These three tune that reconciliation.
    #
    # Set of broadapps `status` values counted as "paid" (CSV or JSON array; compared lower-case).
    # Default "succeeded" (the real broadapps value); parsed by cloudpayments_paid_statuses().
    # Malformed / empty => {"succeeded"}. The actual status is logged each reconcile (Q-054-1).
    cloudpayments_paid_statuses_raw: str = Field(
        default="succeeded", alias="CLOUDPAYMENTS_PAID_STATUSES"
    )
    # Freshness window (hours): only payments with paid_at >= now() - window are creditable, so the
    # first callback for a user with pre-existing history does not credit the whole back-catalogue
    # at once (ADR-054 §Окно свежести). Reference is now() (not the manipulable paid_at); a
    # non-positive value falls back to the default (see the field_validator below).
    cloudpayments_payment_freshness_hours: int = Field(
        default=_CLOUDPAYMENTS_DEFAULT_FRESHNESS_HOURS,
        alias="CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS",
    )
    # Per-source-IP rate limit on the PUBLIC webhook (ADR-054 §1): generous so legitimate
    # callbacks/retries are never throttled; its job is anti-amplification of the outgoing GET.
    cloudpayments_webhook_rate_limit_per_ip: int = Field(
        default=120, alias="CLOUDPAYMENTS_WEBHOOK_RATE_LIMIT_PER_IP"
    )

    # --- CloudPayments (broadapps) RU checkout / payment-link (ADR-051) ---
    # Outgoing call to broadapps POST {base}/payments/link that creates a YooKassa payment link.
    # api_base is PUBLIC (not a secret): the fixed upstream host (no SSRF — never taken from the
    # client body). app_id is the broadapps application UUID (server-side, not in the client).
    cloudpayments_api_base: str = Field(
        default="https://pay.broadapps.dev/api/v1", alias="CLOUDPAYMENTS_API_BASE"
    )
    cloudpayments_app_id: str = Field(default="", alias="CLOUDPAYMENTS_APP_ID")
    # SECRET: outgoing Bearer WE present to broadapps. Semantically distinct from
    # CLOUDPAYMENTS_WEBHOOK_TOKEN (which broadapps presents to US) even if the value currently
    # coincides — separate config allows independent rotation of each side. Empty (default) =>
    # the /checkout endpoint returns 503 (not configured) so it is active only where set (avelyra).
    cloudpayments_api_token: str = Field(default="", alias="CLOUDPAYMENTS_API_TOKEN")

    # --- Token purchase (ADR-015, token-purchase/03) ---
    # Server-side mapping consumable productId -> credits (JSON object). Source of truth for
    # how many credits a token-package purchase grants; never taken from the client body
    # (BR-TP-1 anti-tamper). Example: {"tokens_1500":1500,"tokens_600":600,"tokens_250":250,
    # "tokens_100":100}. Empty default => no products configured (every purchase 422 until set).
    token_products_raw: str = Field(default="{}", alias="TOKEN_PRODUCTS")

    # --- Admin auth (ADR-009, ADM-1) ---
    # Isolated admin secret (X-Admin-Token). High-entropy (>= 32 bytes), only via secret
    # manager / env, never in code/repo/image. Not shared with JWT/KMS/ANTHROPIC/PREVIEW
    # secrets. ADMIN_API_SECRET_PREV is the previous secret kept valid during rotation
    # (grace period); both compared constant-time. Empty (unset) secrets never match.
    admin_api_secret: str = Field(default="", alias="ADMIN_API_SECRET")
    admin_api_secret_prev: str = Field(default="", alias="ADMIN_API_SECRET_PREV")
    admin_rate_limit_per_min: int = Field(default=10, alias="ADMIN_RATE_LIMIT_PER_MIN")
    # Body size limit for admin endpoints (<= 8 KB, ADR-009 §6).
    admin_size_limit_body: int = Field(default=8 * 1024, alias="ADMIN_SIZE_LIMIT_BODY")

    # --- Website builder / preview (ADR-010, ADR-011, WB-2) ---
    # Isolated HMAC secret for signed preview URLs. Separate from JWT/KMS/ADMIN secrets.
    preview_url_secret: str = Field(default="", alias="PREVIEW_URL_SECRET")
    preview_url_ttl_seconds: int = Field(default=900, alias="PREVIEW_URL_TTL_SECONDS")
    preview_max_file_bytes: int = Field(default=1024 * 1024, alias="PREVIEW_MAX_FILE_BYTES")
    preview_max_project_bytes: int = Field(
        default=10 * 1024 * 1024, alias="PREVIEW_MAX_PROJECT_BYTES"
    )
    preview_max_files: int = Field(default=200, alias="PREVIEW_MAX_FILES")
    # Guard against an infinite server-side tool loop (ADR-011 §2).
    max_server_tool_rounds: int = Field(default=16, alias="MAX_SERVER_TOOL_ROUNDS")
    # PUBLIC service host (not a secret; already in Traefik Host labels and .env.prod.example,
    # ADR-017). Read here only to build the ABSOLUTE site.preview URL so the model copies it
    # verbatim instead of hallucinating a host (ADR-031). Empty => relative fallback (dev).
    service_domain: str = Field(default="", alias="SERVICE_DOMAIN")

    # --- Trusted reverse-proxy (X-Forwarded-For parsing, 07-deployment.md) ---
    # API runs behind a reverse-proxy / LB (TLS termination). Only trust XFF/X-Real-IP
    # when the peer is a known proxy; otherwise the header is spoofable. Empty list =>
    # never trust forwarding headers, always use the socket peer (safe default).
    trusted_proxy_ips: str = Field(default="", alias="TRUSTED_PROXY_IPS")
    # Number of trusted proxy hops in front of the app (chained LB/CDN). The client IP is
    # taken (hop_count + 1) entries from the right of X-Forwarded-For. Default 1.
    trusted_proxy_hop_count: int = Field(default=1, alias="TRUSTED_PROXY_HOP_COUNT")

    # --- Rate limits (Q-003-1 defaults, TD-004) ---
    rate_limit_chat_per_user: int = Field(default=30, alias="RATE_LIMIT_CHAT_PER_USER")
    rate_limit_chat_per_device: int = Field(default=60, alias="RATE_LIMIT_CHAT_PER_DEVICE")
    rate_limit_chat_per_ip: int = Field(default=120, alias="RATE_LIMIT_CHAT_PER_IP")
    rate_limit_other_per_user: int = Field(default=60, alias="RATE_LIMIT_OTHER_PER_USER")
    rate_limit_window_seconds: int = Field(default=60, alias="RATE_LIMIT_WINDOW_SECONDS")

    # --- Size limits in bytes (Q-003-2 defaults, TD-004) ---
    size_limit_body: int = Field(default=512 * 1024, alias="SIZE_LIMIT_BODY")
    size_limit_message: int = Field(default=32 * 1024, alias="SIZE_LIMIT_MESSAGE")
    size_limit_context: int = Field(default=64 * 1024, alias="SIZE_LIMIT_CONTEXT")
    size_limit_tool_result: int = Field(default=256 * 1024, alias="SIZE_LIMIT_TOOL_RESULT")
    size_limit_api_key: int = Field(default=4 * 1024, alias="SIZE_LIMIT_API_KEY")

    # --- Inline multimodal attachments (ADR-020, 05-security.md, Q-020-2 defaults) ---
    # Inline base64 attachments are accepted only in the first user message-step of
    # /v1/chat/run. All limits are enforced BEFORE base64 decoding to bound memory use
    # (decoded size ≈ 3/4 of the base64 length). The mediaType allowlist is fixed in code
    # (schemas/chat.py, Q-020-1 governs extension), not env-driven.
    attachment_max_count: int = Field(default=10, alias="ATTACHMENT_MAX_COUNT")
    # Per-attachment decoded-byte ceiling, split by class: image vs document (PDF).
    attachment_max_bytes_image: int = Field(
        default=5 * 1024 * 1024, alias="ATTACHMENT_MAX_BYTES_IMAGE"
    )
    attachment_max_bytes_document: int = Field(
        default=8 * 1024 * 1024, alias="ATTACHMENT_MAX_BYTES_DOCUMENT"
    )
    # Combined decoded-byte ceiling across all attachments in a request.
    attachment_total_bytes: int = Field(default=10 * 1024 * 1024, alias="ATTACHMENT_TOTAL_BYTES")
    # PDF page-count guard (anti decompression/structure bomb) via pypdf.
    attachment_pdf_max_pages: int = Field(default=100, alias="ATTACHMENT_PDF_MAX_PAGES")
    # Raised transport body limit applied ONLY to the /v1/chat/run route (other routes keep
    # size_limit_body). Inline base64 of large files exceeds the general ≤512KB cap.
    attachment_request_body_limit: int = Field(
        default=12 * 1024 * 1024, alias="ATTACHMENT_REQUEST_BODY_LIMIT"
    )

    # --- Workspaces (рабочие пространства) knowledge files (ADR-036 §4/§6) ---
    # Limits for workspace_files (own BYTEA table; ADR-036 §4, TD-027). All defaults are the
    # values fixed in ADR-036 (08 MB per file = the document-cap; 32 MB total per workspace; 20
    # files per workspace). WORKSPACE_CONTEXT_MAX_CHARS bounds the total injected extracted_text
    # (ADR-036 §6) — images are bounded by file count/size, not by this char limit.
    workspace_file_max_count: int = Field(default=20, alias="WORKSPACE_FILE_MAX_COUNT")
    workspace_file_max_bytes: int = Field(default=8 * 1024 * 1024, alias="WORKSPACE_FILE_MAX_BYTES")
    workspace_files_total_bytes: int = Field(
        default=32 * 1024 * 1024, alias="WORKSPACE_FILES_TOTAL_BYTES"
    )
    workspace_context_max_chars: int = Field(default=200_000, alias="WORKSPACE_CONTEXT_MAX_CHARS")
    # Raised transport body limit applied ONLY to the workspace files-upload route
    # (POST /v1/workspaces/{id}/files) — other routes keep size_limit_body (ADR-045).
    # INVARIANT (single source of truth = WORKSPACE_FILE_MAX_BYTES, this limit is derived):
    #   workspace_request_body_limit >= ceil(workspace_file_max_bytes * 4/3) + JSON_OVERHEAD
    # where *4/3 is the base64 inflation of an 8 MB file (≈10.67 MB) and JSON_OVERHEAD is the
    # JSON-envelope slack ({"type","mediaType","filename","data":"..."}, escaping, field headers;
    # recommended >=256 KB). Default 12 MB satisfies it: 10.67 MB + ~1.33 MB slack > 256 KB. Must
    # stay >= the invariant under any operator calibration (TD-004), symmetric to the
    # ATTACHMENT_MAX_BYTES_DOCUMENT <-> ATTACHMENT_REQUEST_BODY_LIMIT relation for /v1/chat/run.
    workspace_request_body_limit: int = Field(
        default=12 * 1024 * 1024, alias="WORKSPACE_REQUEST_BODY_LIMIT"
    )

    # --- DB connection pool (02-tech-stack.md, sized for ~10k users / 2-3 replicas) ---
    # Per-process pool. Effective max conns ≈ (pool_size + max_overflow) * workers * replicas;
    # keep below Postgres max_connections. architect documents the sizing math in docs.
    db_pool_size: int = Field(default=10, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=5, alias="DB_MAX_OVERFLOW")
    db_pool_timeout: float = Field(default=30.0, alias="DB_POOL_TIMEOUT")
    db_pool_recycle: int = Field(default=1800, alias="DB_POOL_RECYCLE")

    # --- Session (Q-001-1) ---
    session_soft_ttl_seconds: int = Field(default=24 * 3600, alias="SESSION_SOFT_TTL_SECONDS")

    # --- Wallet ---
    wallet_last_transactions: int = Field(default=20, alias="WALLET_LAST_TRANSACTIONS")

    # --- Policy cache ---
    policy_cache_ttl_seconds: int = Field(default=5, alias="POLICY_CACHE_TTL_SECONDS")

    # --- API documentation (08-api-documentation.md, R7) ---
    # Toggles /docs, /redoc, /openapi.json. Default true (dev/CI/staging). Recommended
    # false in prod so the API surface is not publicly exposed (05-security.md).
    docs_enabled: bool = Field(default=True, alias="DOCS_ENABLED")

    # --- Prompt presets localization (ADR-049) ---
    # Per-instance default locale for GET /v1/presets (avelyra=ru, others=en). Public, not a
    # secret (ADR-017). Default "en" = current behavior (unset env → EN, backward-compatible).
    # A value outside SUPPORTED_PRESET_LOCALES degrades gracefully to "en" (+ WARNING log), never
    # a startup crash — read via resolved_presets_default_locale(), not the raw field.
    presets_default_locale: str = Field(default="en", alias="PRESETS_DEFAULT_LOCALE")

    # --- Observability ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    otel_exporter_otlp_endpoint: str = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    metrics_scrape_token: str = Field(default="", alias="METRICS_SCRAPE_TOKEN")

    def token_products(self) -> dict[str, int]:
        """Parse TOKEN_PRODUCTS (JSON object productId->credits) into a validated mapping.

        Only string keys with positive-int credit values survive (ADR-015, BR-TP-1). A
        malformed JSON document or non-object yields an empty mapping (every purchase then
        fails 422), never a partial/ambiguous credit table. Pure (no I/O); cached via
        get_settings()'s lru_cache for the process lifetime.
        """
        import json

        try:
            parsed = json.loads(self.token_products_raw or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        products: dict[str, int] = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                continue
            # bool is a subclass of int; exclude it explicitly to avoid True->1 surprises.
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if value <= 0:
                continue
            products[key] = value
        return products

    def adapty_product_tokens(self) -> dict[str, int]:
        """Parse ADAPTY_PRODUCT_TOKENS (JSON object vendor_product_id->tokens) (ADR-029 §5).

        Mirrors token_products(): only string keys with positive-int values survive (bool is a
        subclass of int and is excluded). A malformed JSON document or non-object yields an empty
        mapping, in which case every vendor_product_id falls back to
        adapty_subscription_tokens_grant. Pure (no I/O); cached via get_settings()'s lru_cache.
        """
        import json

        try:
            parsed = json.loads(self.adapty_product_tokens_raw or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        products: dict[str, int] = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                continue
            # bool is a subclass of int; exclude it explicitly to avoid True->1 surprises.
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if value <= 0:
                continue
            products[key] = value
        return products

    def cloudpayments_product_tokens(self) -> dict[str, int]:
        """Parse CLOUDPAYMENTS_PRODUCT_TOKENS (JSON object productId->credits) (ADR-050 §3a).

        Mirrors token_products()/adapty_product_tokens(): only string keys with positive-int values
        survive (bool is a subclass of int and is excluded). A malformed JSON document or non-object
        yields an empty mapping, in which case every subscription product falls back to
        cloudpayments_subscription_tokens_grant. Pure (no I/O); cached via get_settings()'s cache.
        """
        import json

        try:
            parsed = json.loads(self.cloudpayments_product_tokens_raw or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        products: dict[str, int] = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                continue
            # bool is a subclass of int; exclude it explicitly to avoid True->1 surprises.
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if value <= 0:
                continue
            products[key] = value
        return products

    @field_validator("cloudpayments_payment_freshness_hours")
    @classmethod
    def _clamp_freshness_hours(cls, value: int) -> int:
        """A non-positive CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS falls back to the default (ADR-054).

        The window must be strictly positive (``paid_at >= now() - timedelta(hours=window)``); a
        mis-configured ``0``/negative env degrades to the safe default instead of disabling the
        window, mirroring the graceful config parsing elsewhere (token_products()/allowed_models()).
        """
        return value if value > 0 else _CLOUDPAYMENTS_DEFAULT_FRESHNESS_HOURS

    def cloudpayments_paid_statuses(self) -> frozenset[str]:
        """Parse CLOUDPAYMENTS_PAID_STATUSES into the set of "paid" broadapps statuses (ADR-054 §4).

        Accepts a JSON array (``["succeeded","paid"]``) OR a CSV (``succeeded,paid``); each entry is
        stripped and lower-cased (the reconciliation compares ``status.strip().lower()``). A
        malformed / empty value yields ``{"succeeded"}`` (the authoritative broadapps "paid" value)
        so the gate is never accidentally emptied. Pure; cached via get_settings()'s lru_cache.
        """
        import json

        raw = (self.cloudpayments_paid_statuses_raw or "").strip()
        if not raw:
            return frozenset({"succeeded"})
        statuses: set[str] = set()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, str) and item.strip():
                        statuses.add(item.strip().lower())
        else:
            for part in raw.split(","):
                token = part.strip().lower()
                if token:
                    statuses.add(token)
        return frozenset(statuses) if statuses else frozenset({"succeeded"})

    def cloudpayments_checkout_configured(self) -> bool:
        """True when the RU checkout endpoint is configured on this instance (ADR-051 §5).

        Requires BOTH the broadapps application id and the outgoing API token; either empty =>
        POST /v1/billing/cloudpayments/checkout returns 503 (feature not available here). Active
        only on the instance where the operator sets both (avelyra).
        """
        return bool(self.cloudpayments_app_id and self.cloudpayments_api_token)

    def default_model(self) -> str:
        """Active instance default model (ADR-034 §1): the model used when none is selected.

        Provider-aware: ``openai_model`` when ``LLM_PROVIDER=openai``, otherwise ``anthropic_model``
        (the default). This is the model the active client falls back to
        (``settings.<provider>_model``) when ``create_message(model=None)`` — so it is, by
        construction, ALWAYS present in
        ``allowed_models()`` (the empty-allowlist fallback returns exactly this model; a non-empty
        allowlist without it has it prepended at the API layer — GET /v1/models).
        """
        if self.llm_provider.strip().lower() == "openai":
            return self.openai_model
        return self.anthropic_model

    def byok_default_model_for(self, provider: str) -> str:
        """BYOK default model for a SPECIFIC provider (ADR-044 §5/§6, ADR-016).

        ``"openai"`` → ``openai_byok_default_model``; any non-openai value (incl. ``"anthropic"``) →
        ``byok_default_model``. This is the model reported as ``activeModel`` when keyStatus=valid
        and the model used for BYOK generation when the session model is absent / belongs to another
        provider (ADR-044 §5.3). Provider-aware, independent of ``LLM_PROVIDER``.
        """
        if provider.strip().lower() == "openai":
            return self.openai_byok_default_model
        return self.byok_default_model

    def resolved_deep_thinking_effort(self) -> str:
        """Reasoning effort for Deep Thinking, validated gracefully (ADR-055/ADR-059 §3).

        Normalizes ``deep_thinking_effort`` (``strip().lower()``) and returns it when it is one of
        ``{low, medium, high}``; any other value degrades to ``"medium"`` — a mis-configured env
        falls back to a safe default instead of sending an invalid ``reasoning.effort`` to OpenAI
        (mirrors ``resolved_presets_default_locale``/``token_products``). Pure; cached via
        get_settings()'s lru_cache.
        """
        value = self.deep_thinking_effort.strip().lower()
        return value if value in _REASONING_EFFORT_VALUES else "medium"

    def resolved_search_context_size(self) -> str:
        """search_context_size for the web_search tool, validated gracefully (ADR-055/ADR-059 §2).

        Normalizes ``openai_search_context_size`` (``strip().lower()``) and returns it when it is
        one of ``{low, medium, high}``; any other value degrades to ``"medium"``. Same graceful
        pattern as ``resolved_deep_thinking_effort``. Pure; cached via get_settings()'s lru_cache.
        """
        value = self.openai_search_context_size.strip().lower()
        return value if value in _SEARCH_CONTEXT_SIZE_VALUES else "medium"

    def resolved_image_size(self) -> str:
        """Instance default image size, validated gracefully (ADR-058). A value outside
        ``IMAGE_SIZE_VALUES`` degrades to ``1024x1024`` — a mis-configured env falls back to a safe
        default instead of sending an invalid ``size`` to OpenAI (mirrors
        ``resolved_deep_thinking_effort``). Pure; cached via get_settings()'s lru_cache.
        """
        value = self.image_default_size.strip().lower()
        return value if value in IMAGE_SIZE_VALUES else _IMAGE_DEFAULT_SIZE

    def resolved_image_quality(self) -> str:
        """Instance default image quality, validated gracefully (ADR-058). A value outside
        ``IMAGE_QUALITY_VALUES`` degrades to ``high``. Pure; cached via get_settings()'s lru_cache.
        """
        value = self.image_default_quality.strip().lower()
        return value if value in IMAGE_QUALITY_VALUES else _IMAGE_DEFAULT_QUALITY

    def resolved_image_output_format(self) -> str:
        """Instance default image output format, validated gracefully (ADR-058). A value outside
        ``IMAGE_OUTPUT_FORMAT_VALUES`` degrades to ``png``. Pure; cached via get_settings()'s cache.
        """
        value = self.image_output_format.strip().lower()
        return value if value in IMAGE_OUTPUT_FORMAT_VALUES else _IMAGE_DEFAULT_OUTPUT_FORMAT

    def allowed_models(self) -> dict[str, str]:
        """Active provider's model allowlist as a validated {id: displayName} mapping (ADR-034 §1).

        Thin wrapper over :meth:`allowed_models_for` for the ACTIVE provider (``LLM_PROVIDER``,
        default anthropic). Signature and behavior are unchanged — existing callers keep working.
        """
        return self.allowed_models_for(self.llm_provider.strip().lower())

    def allowed_models_for(self, provider: str) -> dict[str, str]:
        """Parse a SPECIFIC provider's model allowlist into a validated {id: displayName} mapping.

        Provider-aware (ADR-034 §1, generalized for ADR-044 §5): reads ``openai_models_raw`` for
        ``"openai"``, else ``anthropic_models_raw`` (any non-openai value, incl. ``"anthropic"``).
        Used by the multi-provider BYOK path to check a session model against the allowlist of the
        KEY's provider (not the active one). Same shape rules as ``token_products()``: only ``str``
        keys with a non-empty ``str`` value survive (key stripped to a non-empty string; value a
        non-empty string after the emptiness check). A malformed JSON document or a non-object
        yields an empty mapping.

        Backward-compatibility fallback: when the parsed result is empty, returns
        ``{default: default}`` — a single entry equal to that provider's default model
        (``<provider>_model``, displayName = id). So an unset allowlist reproduces the current
        behavior exactly (one model, the provider default).

        Invariant (ADR-034 §1): the provider's default model is ALWAYS present in the result. When a
        non-empty allowlist does NOT contain it, the default is PREPENDED (displayName = id, first
        key); the rest keep the allowlist insertion order. Pure (no I/O); cached via get_settings().
        """
        import json

        is_openai = provider.strip().lower() == "openai"
        raw = self.openai_models_raw if is_openai else self.anthropic_models_raw
        try:
            parsed = json.loads(raw or "{}")
        except (ValueError, json.JSONDecodeError):
            parsed = {}
        parsed_models: dict[str, str] = {}
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                if not isinstance(key, str):
                    continue
                stripped_key = key.strip()
                if not stripped_key:
                    continue
                # bool is a subclass of int (not str); the isinstance(str) check excludes it.
                if not isinstance(value, str) or not value:
                    continue
                parsed_models[stripped_key] = value
        default = self.openai_model if is_openai else self.anthropic_model
        if not parsed_models:
            # Empty allowlist → backward-compatible single default entry (displayName = id).
            return {default: default}
        if default in parsed_models:
            return parsed_models
        # Non-empty allowlist missing the default → prepend the default first (invariant §1),
        # keeping the allowlist's insertion order for the rest.
        return {default: default, **parsed_models}

    @staticmethod
    def _resolve_pem(path_value: str, string_value: str) -> str:
        """Resolve a PEM key: file path takes priority over the \\n-escaped string (ADR-018 §7).

        When a path is set it is read from disk verbatim (recommended prod: mounted secret, no
        escaping). Otherwise the env string value has literal ``\\n`` sequences turned into real
        newlines so a single-line .env value yields a valid multi-line PEM. Empty when neither is
        configured. Never logs the key material (redaction covers ``*key*``).
        """
        if path_value:
            with open(path_value, encoding="utf-8") as handle:
                return handle.read()
        if string_value:
            return string_value.replace("\\n", "\n")
        return ""

    def resolve_private_key(self) -> str:
        """Private RS256 signing key PEM, or '' if the issuer is not configured (=> 503)."""
        return self._resolve_pem(self.jwt_private_key_path, self.jwt_private_key)

    def resolve_public_key(self) -> str:
        """Public RS256 verification key PEM (used by JwtVerifier and the JWKS endpoint)."""
        return self._resolve_pem(self.jwt_public_key_path, self.jwt_public_key)

    def apple_audience_resolved(self) -> str:
        """Effective Apple `aud` for verification (ADR-043 §3).

        Returns ``apple_audience`` (stripped) if set, else ``appstore_bundle_id`` (stripped) as a
        fallback (if a bundle id is already configured for StoreKit it doubles as the Apple
        audience), else ``""``. An empty result means Apple sign-in is "not configured" — the
        router returns 503 (operational misconfiguration, not a client error). Pure (no I/O).
        """
        explicit = self.apple_audience.strip()
        if explicit:
            return explicit
        return self.appstore_bundle_id.strip()

    def normalized_service_domain(self) -> str:
        """Return SERVICE_DOMAIN as a bare host[:port] for the absolute preview URL (ADR-031).

        Strips a leading http(s):// scheme (case-insensitive) and surrounding slashes so the
        value is the same host regardless of how it is set (``broadnova.shop``,
        ``https://broadnova.shop`` or ``broadnova.shop/``). Returns '' when unset/blank, which
        the caller treats as "not configured" => relative fallback. Snapping the trailing slash
        guarantees the assembled URL has no double slash before ``/v1/``.
        """
        value = self.service_domain.strip()
        lowered = value.lower()
        if lowered.startswith("https://"):
            value = value[len("https://") :]
        elif lowered.startswith("http://"):
            value = value[len("http://") :]
        value = value.strip("/")
        return value

    def resolved_presets_default_locale(self) -> str:
        """Per-instance default locale for GET /v1/presets, validated gracefully (ADR-049 §4).

        Normalizes ``presets_default_locale`` (``strip().lower()``) and returns it if it is in
        ``SUPPORTED_PRESET_LOCALES``. A value outside the set degrades to ``DEFAULT_PRESET_LOCALE``
        (``"en"``) and logs a WARNING — mis-configured env falls back to a safe default instead of
        crashing the process, mirroring ``token_products()``/``allowed_models_for()`` (ADR-034 §1).
        Pure (no I/O). Cached via get_settings()'s lru_cache; the WARNING fires once per process.
        """
        from app.chat.presets import DEFAULT_PRESET_LOCALE, SUPPORTED_PRESET_LOCALES
        from app.observability.logging import get_logger

        normalized = self.presets_default_locale.strip().lower()
        if normalized in SUPPORTED_PRESET_LOCALES:
            return normalized
        get_logger("app.config").warning(
            "PRESETS_DEFAULT_LOCALE=%r is not a supported locale; falling back to %r",
            self.presets_default_locale,
            DEFAULT_PRESET_LOCALE,
        )
        return DEFAULT_PRESET_LOCALE

    def trusted_proxy_networks(self) -> tuple[_IpNetwork, ...]:
        """Parse TRUSTED_PROXY_IPS (comma-separated IPs/CIDRs) into networks.

        Invalid entries are skipped. Empty/blank => empty tuple (never trust XFF).
        """
        networks: list[_IpNetwork] = []
        for raw in self.trusted_proxy_ips.split(","):
            entry = raw.strip()
            if not entry:
                continue
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                continue
        return tuple(networks)


# Content-type allowlist for site_files (ADR-010, website-builder/05-security.md). Only these
# types may be stored and served by the preview endpoint. Fixed on the server (not configurable
# at runtime to keep the threat model deterministic; Q-010-2 leaves the exact list to architect).
PREVIEW_CONTENT_TYPE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "text/html",
        "text/css",
        "text/javascript",
        "application/json",
        "image/png",
        "image/jpeg",
        "image/svg+xml",
        "image/gif",
        "image/webp",
        "font/woff2",
        "text/plain",
    }
)


@lru_cache
def get_settings() -> Settings:
    return Settings()
