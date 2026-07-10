"""Gateway middleware: size limit, correlation id, security headers (api-gateway/03)."""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.config import get_settings
from app.observability.context import set_request_id, set_session_id, set_user_id


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Generates/propagates X-Request-Id (HTTP correlation id, NOT a billing key, ADR-005)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        set_request_id(request_id)
        set_session_id(None)
        set_user_id(None)
        request.state.request_id = request_id
        response: Response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


class SizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejects bodies exceeding the limit before parsing (413).

    The general limit applies to all routes. Two routes get a RAISED transport limit because they
    accept large base64 payloads that exceed the general ≤512KB cap; each raise is scoped to its
    own route so the attack surface for accepting a large payload is not widened globally:
      - /v1/chat/run — inline base64 attachments (ADR-020, 05-security.md);
      - POST /v1/workspaces/{id}/files — base64 workspace knowledge-file upload (ADR-045). Matched
        by path prefix+suffix (the path carries the workspace id), method-agnostic like the
        /v1/chat/run rule: GET /v1/workspaces/{id}/files also matches but carries no body, so the
        raised limit is harmless for it.
    """

    _CHAT_RUN_PATH = "/v1/chat/run"
    _WORKSPACES_PREFIX = "/v1/workspaces/"
    _FILES_SUFFIX = "/files"

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        settings = get_settings()
        self._limit = settings.size_limit_body
        self._chat_run_limit = settings.attachment_request_body_limit
        self._workspace_files_limit = settings.workspace_request_body_limit

    def _limit_for(self, path: str) -> int:
        if path == self._CHAT_RUN_PATH:
            return self._chat_run_limit
        if path.startswith(self._WORKSPACES_PREFIX) and path.endswith(self._FILES_SUFFIX):
            return self._workspace_files_limit
        return self._limit

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._limit_for(request.url.path):
                    return self._too_large(request)
            except ValueError:
                pass
        return await call_next(request)

    def _too_large(self, request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "payload_too_large",
                    "message": "request body exceeds limit",
                    "requestId": request_id,
                }
            },
        )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Default API security headers.

    The preview endpoint (/v1/preview/*) serves user (Claude-generated) HTML/JS and needs its own
    sandbox headers (CSP sandbox, X-Frame-Options: SAMEORIGIN, no-store; ADR-010) which differ from
    the API defaults (notably X-Frame-Options: DENY). The middleware therefore does NOT set its
    defaults on preview paths — the preview route owns its complete header set.
    """

    _PREVIEW_PREFIX = "/v1/preview/"

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response: Response = await call_next(request)
        if request.url.path.startswith(self._PREVIEW_PREFIX):
            return response
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response
