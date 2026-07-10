"""Integration: OpenAPI/Swagger documentation convention (08-api-documentation.md).

Covers follow_up_for_qa from backend:
1. DOCS_ENABLED toggles /docs, /redoc, /openapi.json (404 when off, 200 when on/default).
2. openapi.json: /v1/* require bearerAuth; /health,/ready,/metrics have no security;
   components.securitySchemes.bearerAuth = {type:http, scheme:bearer, bearerFormat:JWT}.
3. Real JWT verification unchanged (auto_error=False on HTTPBearer did NOT break
   get_current_user): no/broken Bearer on /v1/* still 401 (regression guard, CRITICAL).
4. Named request/response examples on chat/run, chat/tool-result, byok/set, wallet/consume.
5. blockReason documents all 8 ADR-004 values in ChatResponse; policy reasons[] references them.
6. Tag order Chat, Policy, Wallet, Subscription, BYOK, Health; each endpoint has exactly one tag.

The documentation layer is reflection-only: these tests use the OpenAPI schema produced by
create_app() and (for the regression guard) the live ASGI client from conftest. DOCS_ENABLED
variants build dedicated apps with the flag overridden via the lru_cached settings.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user

if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.config import Settings

# IMPORTANT: do NOT import app.main / call get_settings at module top.
# `app.main` runs `create_app()` (→ get_settings()) at import time; importing it during
# collection would cache the default localhost DATABASE_URL before the testcontainer env
# is set (conftest sets DATABASE_URL inside the session-scoped pg_url fixture), poisoning
# the lru_cached settings for alembic migrations and the app.db engine. All app imports
# below are deferred into fixtures/helpers, mirroring conftest's lazy-import pattern.

# ADR-004 canonical blockReason values (docs/adr/ADR-004-blocked-http-200.md).
_BLOCK_REASONS = (
    "trial_used",
    "subscription_required",
    "subscription_expired",
    "credits_empty",
    "byok_disabled",
    "byok_invalid",
    "rate_limited",
    "policy_denied",
)

# Expected tag order per R4 — kept in lock-step with the openapi_tags declaration order in
# app.main (_OPENAPI_TAGS). Verified against main.py: the embedded auth-issuer (ADR-018) prepends
# `Auth`, the tools-catalog (ADR-019) inserts `Tools` after `Chat`, and `Health` is appended last
# by the health router. Tokens (consumable IAP, ADR-015) sits between Subscription and BYOK.
_TAG_ORDER = [
    "Auth",
    "Chat",
    "Tools",
    "Models",
    "Presets",
    "Policy",
    "Wallet",
    "Subscription",
    "Tokens",
    "BYOK",
    "Admin",
    "Preview",
    "Chats",
    # ADR-058: the Images tag (GET /v1/images) is declared in _OPENAPI_TAGS right after Chats and
    # before Workspaces (main.py) — images are a per-chat artifact, so they sit next to Chats.
    "Images",
    "Workspaces",
    "Profile",
    "Preferences",
    "Health",
]

# Endpoint -> expected single tag (R4 table).
_ENDPOINT_TAG = {
    ("/v1/auth/register", "post"): "Auth",
    ("/v1/auth/token", "post"): "Auth",
    ("/v1/auth/refresh", "post"): "Auth",
    ("/v1/auth/jwks", "get"): "Auth",
    ("/v1/chat/run", "post"): "Chat",
    ("/v1/chat/tool-result", "post"): "Chat",
    ("/v1/tools", "get"): "Tools",
    ("/v1/models", "get"): "Models",
    ("/v1/presets", "get"): "Presets",
    ("/v1/policy/effective", "get"): "Policy",
    ("/v1/wallet", "get"): "Wallet",
    ("/v1/wallet/consume", "post"): "Wallet",
    ("/v1/subscription/sync", "post"): "Subscription",
    ("/v1/byok/set", "post"): "BYOK",
    ("/v1/byok/toggle", "post"): "BYOK",
    ("/v1/byok/delete", "post"): "BYOK",
    # Workspaces (ADR-036): 8 owner-scoped CRUD + knowledge-file endpoints, all tag=Workspaces.
    ("/v1/workspaces", "post"): "Workspaces",
    ("/v1/workspaces", "get"): "Workspaces",
    ("/v1/workspaces/{workspace_id}", "get"): "Workspaces",
    ("/v1/workspaces/{workspace_id}", "patch"): "Workspaces",
    ("/v1/workspaces/{workspace_id}", "delete"): "Workspaces",
    ("/v1/workspaces/{workspace_id}/files", "post"): "Workspaces",
    ("/v1/workspaces/{workspace_id}/files", "get"): "Workspaces",
    ("/v1/workspaces/{workspace_id}/files/{file_id}", "delete"): "Workspaces",
    ("/health", "get"): "Health",
    ("/ready", "get"): "Health",
    ("/metrics", "get"): "Health",
}

# Public service endpoints that must NOT carry a security requirement.
_PUBLIC_PATHS = {"/health", "/ready", "/metrics"}

# Public auth-issuer endpoints (ADR-018 §2): obtaining the token => no user JWT requirement.
_AUTH_PUBLIC_PATHS = {
    ("/v1/auth/register", "post"),
    ("/v1/auth/token", "post"),
    ("/v1/auth/refresh", "post"),
    ("/v1/auth/jwks", "get"),
}

# Admin endpoints (ADR-009): authorized by the isolated adminToken scheme, NOT bearerAuth.
_ADMIN_PATHS = {
    ("/v1/admin/wallet/grant", "post"),
    ("/v1/admin/wallet/{userId}", "get"),
}


# --------------------------- app/openapi builders ---------------------------
def _build_app(*, docs_enabled: bool) -> FastAPI:
    """Build a fresh app with DOCS_ENABLED overridden.

    Must NOT clear the lru_cached settings: the real cache (incl. the testcontainer
    DATABASE_URL established by conftest) backs `app.db`'s lazily-built global engine,
    which `/ready` and `/health` use. We derive an override copy from the existing cached
    Settings and patch only the `get_settings` symbol that `create_app()` resolves
    (app.config + app.main re-export), restoring it afterwards. `app.db` binds the real
    function object directly, so it is unaffected by this patch.
    """
    import app.config as config_mod
    import app.main as main_mod

    overridden = config_mod.get_settings().model_copy(update={"docs_enabled": docs_enabled})

    def _override() -> Settings:
        return overridden

    config_get = config_mod.get_settings
    main_get = main_mod.get_settings
    config_mod.get_settings = _override  # type: ignore[assignment]
    main_mod.get_settings = _override  # type: ignore[assignment]
    try:
        return main_mod.create_app()
    finally:
        config_mod.get_settings = config_get  # type: ignore[assignment]
        main_mod.get_settings = main_get  # type: ignore[assignment]


@pytest.fixture(scope="module")
def openapi_schema(pg_url: str) -> dict[str, Any]:
    """OpenAPI schema from a docs-enabled app (default state).

    Depends on pg_url so the testcontainer DATABASE_URL is in env before any settings
    read, keeping the shared lru_cached Settings (and app.db engine) consistent.
    """
    app = _build_app(docs_enabled=True)
    return app.openapi()


def _operation(schema: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    return schema["paths"][path][method]


def _security_scheme_names(operation: dict[str, Any]) -> list[str]:
    """Flatten the security requirement list to the referenced scheme names."""
    names: list[str] = []
    for requirement in operation.get("security", []):
        names.extend(requirement.keys())
    return names


# ============================================================================
# 1. DOCS_ENABLED toggle (R7)
# ============================================================================
@pytest.mark.asyncio
async def test_docs_enabled_default_true_serves_docs(pg_url: str) -> None:
    # Default settings have docs_enabled=True.
    from app.config import get_settings

    assert get_settings().docs_enabled is True


@pytest.mark.asyncio
async def test_docs_enabled_true_endpoints_return_200(pg_url: str) -> None:
    app = _build_app(docs_enabled=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for path in ("/docs", "/redoc", "/openapi.json"):
            r = await ac.get(path)
            assert r.status_code == 200, f"{path} expected 200, got {r.status_code}"


@pytest.mark.asyncio
async def test_docs_enabled_false_endpoints_return_404(pg_url: str) -> None:
    app = _build_app(docs_enabled=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for path in ("/docs", "/redoc", "/openapi.json"):
            r = await ac.get(path)
            assert r.status_code == 404, f"{path} expected 404, got {r.status_code}"


@pytest.mark.asyncio
async def test_docs_disabled_does_not_break_functional_endpoints(pg_url: str) -> None:
    # Disabling docs must not affect real routes: /health still works.
    app = _build_app(docs_enabled=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ============================================================================
# 2. Security scheme declaration (R2)
# ============================================================================
def test_bearer_security_scheme_declared(openapi_schema: dict[str, Any]) -> None:
    schemes = openapi_schema["components"]["securitySchemes"]
    assert "bearerAuth" in schemes
    bearer = schemes["bearerAuth"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    assert bearer["bearerFormat"] == "JWT"


def test_security_scheme_has_russian_description(openapi_schema: dict[str, Any]) -> None:
    bearer = openapi_schema["components"]["securitySchemes"]["bearerAuth"]
    desc = bearer.get("description", "")
    assert "JWT" in desc and "sub" in desc
    assert "userId" in desc  # explains the 403 contract


def test_admin_token_security_scheme_declared(openapi_schema: dict[str, Any]) -> None:
    # ADR-009: the isolated admin authorization is reflected as the `adminToken` apiKey-in-header
    # scheme so Swagger shows the lock for /v1/admin/* — alongside bearerAuth.
    schemes = openapi_schema["components"]["securitySchemes"]
    assert "bearerAuth" in schemes
    assert "adminToken" in schemes
    admin = schemes["adminToken"]
    assert admin["type"] == "apiKey"
    assert admin["in"] == "header"
    assert admin["name"] == "X-Admin-Token"


@pytest.mark.parametrize(
    ("path", "method"),
    # bearerAuth covers user /v1/* endpoints EXCEPT the public auth-issuer routes (ADR-018) and the
    # admin routes (adminToken, ADR-009). /v1/tools (ADR-019) is bearer-protected like other reads.
    [
        (p, m)
        for (p, m), tag in _ENDPOINT_TAG.items()
        if p.startswith("/v1/") and (p, m) not in _AUTH_PUBLIC_PATHS
    ],
)
def test_v1_endpoints_require_bearer_auth(
    openapi_schema: dict[str, Any], path: str, method: str
) -> None:
    op = _operation(openapi_schema, path, method)
    assert op.get("security") == [
        {"bearerAuth": []}
    ], f"{method.upper()} {path} security != [{{'bearerAuth': []}}]: {op.get('security')}"


def test_tools_endpoint_requires_bearer_auth(openapi_schema: dict[str, Any]) -> None:
    # ADR-019: GET /v1/tools is JWT-protected like all /v1/* reads.
    op = _operation(openapi_schema, "/v1/tools", "get")
    assert op.get("security") == [{"bearerAuth": []}], op.get("security")


@pytest.mark.parametrize(("path", "method"), sorted(_AUTH_PUBLIC_PATHS))
def test_auth_endpoints_have_no_security(
    openapi_schema: dict[str, Any], path: str, method: str
) -> None:
    # ADR-018 §2: /v1/auth/* are public (this is where the token is obtained) — no lock icon.
    op = _operation(openapi_schema, path, method)
    assert not op.get(
        "security"
    ), f"{method.upper()} {path} must be public, got {op.get('security')}"


@pytest.mark.parametrize(("path", "method"), sorted(_ADMIN_PATHS))
def test_admin_endpoints_require_admin_token_only(
    openapi_schema: dict[str, Any], path: str, method: str
) -> None:
    # ADR-009: /v1/admin/* authorize via adminToken ONLY; the user JWT is not an auth factor.
    op = _operation(openapi_schema, path, method)
    assert op.get("security") == [
        {"adminToken": []}
    ], f"{method.upper()} {path} security != [{{'adminToken': []}}]: {op.get('security')}"


@pytest.mark.parametrize("path", sorted(_PUBLIC_PATHS))
def test_public_endpoints_have_no_security(openapi_schema: dict[str, Any], path: str) -> None:
    op = _operation(openapi_schema, path, "get")
    # No lock icon: security must be absent or empty.
    assert not op.get("security"), f"{path} must not require auth, got {op.get('security')}"


def test_no_global_security_applied(openapi_schema: dict[str, Any]) -> None:
    # Security is per-operation (so Health stays public); no document-level requirement.
    assert not openapi_schema.get("security")


# ============================================================================
# 2b. No duplicate auth PARAMETERS (regression guard for the dedup fix).
#     Auth headers (Authorization / X-Admin-Token) must surface ONLY as
#     securitySchemes, NEVER as operation `parameters`. Before the fix the
#     routers declared Header()/Depends() that emitted a duplicate header param
#     in Swagger alongside the Authorize lock. SecurityBase schemes
#     (bearer_scheme / admin_scheme) replaced that, so the schema must now be
#     free of those header parameters on EVERY operation. (src/app/deps.py,
#     src/app/api_gateway/auth.py, src/app/api_gateway/openapi_security.py.)
# ============================================================================
# Header param names (lower-cased) that are auth and must NOT appear as parameters —
# they belong exclusively to components.securitySchemes (bearerAuth / adminToken).
_FORBIDDEN_AUTH_PARAM_NAMES = {"authorization", "x-admin-token"}


def _all_operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Every (path, method, operation) in the document for exhaustive scanning."""
    ops: list[tuple[str, str, dict[str, Any]]] = []
    for path, item in schema.get("paths", {}).items():
        for method, operation in item.items():
            if method in {"get", "post", "put", "patch", "delete", "options", "head"}:
                ops.append((path, method, operation))
    return ops


def _header_param_names(operation: dict[str, Any]) -> list[str]:
    """Lower-cased names of all `in: header` parameters declared on the operation."""
    return [
        str(p.get("name", "")).lower()
        for p in operation.get("parameters", [])
        if p.get("in") == "header"
    ]


def test_no_operation_declares_auth_header_as_parameter(openapi_schema: dict[str, Any]) -> None:
    # MAIN regression guard: no operation anywhere may carry `authorization` or `X-Admin-Token`
    # as a parameter (case-insensitive). They are securitySchemes only — a parameter here means
    # the duplicate-auth-field bug regressed.
    offenders: list[str] = []
    for path, method, operation in _all_operations(openapi_schema):
        dup = set(_header_param_names(operation)) & _FORBIDDEN_AUTH_PARAM_NAMES
        if dup:
            offenders.append(f"{method.upper()} {path}: {sorted(dup)}")
    assert not offenders, "auth headers leaked into operation parameters (dup-auth regressed): " + (
        "; ".join(offenders)
    )


def test_authorization_not_a_parameter_on_protected_endpoints(
    openapi_schema: dict[str, Any],
) -> None:
    # Spot-check the JWT-protected operations specifically: the lock comes from `security`,
    # never from an `authorization` header parameter.
    for path, method in _ENDPOINT_TAG:
        if not path.startswith("/v1/") or (path, method) in _AUTH_PUBLIC_PATHS:
            continue
        op = _operation(openapi_schema, path, method)
        assert "authorization" not in _header_param_names(
            op
        ), f"{method.upper()} {path} must not declare an `authorization` parameter"


def test_admin_token_not_a_parameter_on_admin_endpoints(openapi_schema: dict[str, Any]) -> None:
    # /v1/admin/* authorize via the adminToken securityScheme; X-Admin-Token must NOT be a param.
    for path, method in _ADMIN_PATHS:
        op = _operation(openapi_schema, path, method)
        assert "x-admin-token" not in _header_param_names(
            op
        ), f"{method.upper()} {path} must not declare an `X-Admin-Token` parameter"


# ============================================================================
# 2c. Legitimate header params survive the dedup (must NOT be over-pruned).
#     X-Device-Id is a real Header() on the chat endpoints (rate-limit device
#     scoping); removing auth params must leave it intact. (routers/chat.py.)
# ============================================================================
@pytest.mark.parametrize("path", ["/v1/chat/run", "/v1/chat/tool-result"])
def test_x_device_id_header_param_preserved_on_chat(
    openapi_schema: dict[str, Any], path: str
) -> None:
    op = _operation(openapi_schema, path, "post")
    names = _header_param_names(op)
    assert "x-device-id" in names, f"{path} lost its legitimate X-Device-Id header param: {names}"
    # And it is genuinely a parameter, not an auth one.
    assert "x-device-id" not in _FORBIDDEN_AUTH_PARAM_NAMES


# ============================================================================
# 3. Real JWT verification regression (R2) — CRITICAL
#    auto_error=False on HTTPBearer must NOT short-circuit get_current_user.
# ============================================================================
@pytest.mark.asyncio
async def test_regression_missing_bearer_still_401(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uuid.uuid4()), "projectId": "p", "message": "hi", "mode": "credits"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_regression_broken_bearer_still_401(client: AsyncClient) -> None:
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uuid.uuid4()), "projectId": "p", "message": "hi", "mode": "credits"},
        headers={"Authorization": "Bearer not.a.real.jwt"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_regression_missing_bearer_401_across_v1_endpoints(client: AsyncClient) -> None:
    # Probe a sample protected endpoint per module to prove the security dep didn't
    # swallow auth anywhere it's declared.
    probes = [
        ("post", "/v1/chat/tool-result"),
        ("get", "/v1/policy/effective"),
        ("get", "/v1/wallet"),
        ("post", "/v1/wallet/consume"),
        ("post", "/v1/subscription/sync"),
        ("post", "/v1/byok/set"),
        ("post", "/v1/byok/toggle"),
        ("post", "/v1/byok/delete"),
    ]
    for method, path in probes:
        if method == "get":
            r = await client.get(path)
        else:
            r = await client.post(path, json={})
        assert r.status_code == 401, f"{method.upper()} {path} expected 401, got {r.status_code}"


@pytest.mark.asyncio
async def test_regression_valid_bearer_passes_auth_not_401(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A well-formed token must NOT be rejected as unauthorized (proves the real verifier
    # still runs and accepts good tokens). The seeded user has trial used and no
    # subscription, so the orchestrator blocks business-side (200) — the point is it
    # is not 401/403.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, trial_used=True)
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "projectId": "p", "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code not in (401, 403)
    assert r.status_code == 200
    assert r.json()["status"] == "blocked"


# ============================================================================
# 4. Named examples (R5)
# ============================================================================
def _request_example_names(op: dict[str, Any]) -> set[str]:
    body = op.get("requestBody", {})
    content = body.get("content", {}).get("application/json", {})
    return set(content.get("examples", {}).keys())


def _response_example_names(op: dict[str, Any], status: str = "200") -> set[str]:
    resp = op.get("responses", {}).get(status, {})
    content = resp.get("content", {}).get("application/json", {})
    return set(content.get("examples", {}).keys())


def test_chat_run_response_examples(openapi_schema: dict[str, Any]) -> None:
    op = _operation(openapi_schema, "/v1/chat/run", "post")
    names = _response_example_names(op)
    assert {"assistant_message", "tool_call", "blocked"} <= names, names


def test_chat_run_request_example(openapi_schema: dict[str, Any]) -> None:
    op = _operation(openapi_schema, "/v1/chat/run", "post")
    assert _request_example_names(op), "chat/run must have a named request example"


def test_chat_tool_result_response_examples(openapi_schema: dict[str, Any]) -> None:
    op = _operation(openapi_schema, "/v1/chat/tool-result", "post")
    names = _response_example_names(op)
    assert "assistant_message" in names, names


def test_chat_tool_result_request_examples(openapi_schema: dict[str, Any]) -> None:
    op = _operation(openapi_schema, "/v1/chat/tool-result", "post")
    names = _request_example_names(op)
    # R5 (ADR-025): parallel batch, single deprecated form, and an error item.
    assert {"batch", "single_deprecated", "error"} <= names, names


def test_byok_set_examples_valid_and_invalid(openapi_schema: dict[str, Any]) -> None:
    op = _operation(openapi_schema, "/v1/byok/set", "post")
    resp_names = _response_example_names(op)
    assert {"valid", "invalid"} <= resp_names, resp_names
    assert _request_example_names(op), "byok/set must have a request example"


def test_byok_set_request_example_marks_redaction(openapi_schema: dict[str, Any]) -> None:
    op = _operation(openapi_schema, "/v1/byok/set", "post")
    body = op["requestBody"]["content"]["application/json"]
    examples = body.get("examples", {})
    # apiKey example must be a placeholder, never a real key, and note redaction.
    blob = str(examples)
    assert "sk-ant-" in blob  # placeholder shape
    assert "логир" in blob or "redact" in blob.lower()


def test_wallet_consume_example_debit_one(openapi_schema: dict[str, Any]) -> None:
    op = _operation(openapi_schema, "/v1/wallet/consume", "post")
    assert "debit_one" in _request_example_names(op)


# ============================================================================
# 5. blockReason / reasons documentation (R3)
# ============================================================================
def _chat_response_schema(openapi_schema: dict[str, Any]) -> dict[str, Any]:
    return openapi_schema["components"]["schemas"]["ChatResponse"]


def test_chat_response_blockreason_documents_all_8(openapi_schema: dict[str, Any]) -> None:
    schema = _chat_response_schema(openapi_schema)
    block_field = schema["properties"]["blockReason"]
    desc = block_field.get("description", "")
    for reason in _BLOCK_REASONS:
        assert reason in desc, f"blockReason description missing '{reason}'"


def test_policy_reasons_references_same_set(openapi_schema: dict[str, Any]) -> None:
    schema = openapi_schema["components"]["schemas"]["EffectivePolicyResponse"]
    reasons_field = schema["properties"]["reasons"]
    desc = reasons_field.get("description", "")
    for reason in _BLOCK_REASONS:
        assert reason in desc, f"policy reasons[] description missing '{reason}'"


def test_chat_response_status_invariant_documented(openapi_schema: dict[str, Any]) -> None:
    schema = _chat_response_schema(openapi_schema)
    desc = schema.get("description", "")
    # Three mutually-exclusive states documented (R3).
    for state in ("assistant_message", "tool_call", "blocked"):
        assert state in desc, f"ChatResponse description missing state '{state}'"


# ============================================================================
# 6. Tags & grouping (R4)
# ============================================================================
def test_tag_order(openapi_schema: dict[str, Any]) -> None:
    declared = [t["name"] for t in openapi_schema.get("tags", [])]
    assert declared == _TAG_ORDER, declared


def test_tags_have_russian_descriptions(openapi_schema: dict[str, Any]) -> None:
    for tag in openapi_schema.get("tags", []):
        assert tag.get("description"), f"tag {tag['name']} has no description"


@pytest.mark.parametrize(
    ("path", "method", "expected_tag"), [(p, m, tag) for (p, m), tag in _ENDPOINT_TAG.items()]
)
def test_each_endpoint_has_exactly_one_correct_tag(
    openapi_schema: dict[str, Any], path: str, method: str, expected_tag: str
) -> None:
    op = _operation(openapi_schema, path, method)
    tags = op.get("tags", [])
    assert tags == [expected_tag], f"{method.upper()} {path} tags={tags}, expected [{expected_tag}]"


def test_all_documented_paths_have_summary_and_description(openapi_schema: dict[str, Any]) -> None:
    for path, method in _ENDPOINT_TAG:
        op = _operation(openapi_schema, path, method)
        assert op.get("summary"), f"{method.upper()} {path} missing summary"
        assert op.get("description"), f"{method.upper()} {path} missing description"


# ============================================================================
# R6. API metadata
# ============================================================================
def test_api_metadata(openapi_schema: dict[str, Any]) -> None:
    info = openapi_schema["info"]
    assert info["title"] == "claude-ios-backend"
    assert info["version"] == "0.1.0"
    desc = info.get("description", "")
    # Russian context: auth + blocked=200 rule referenced (R6).
    assert "JWT" in desc
    assert "200" in desc  # blocked=HTTP 200 mentioned
