"""Unit tests for the per-path workspace files-upload body limit (ADR-045).

ADR-045 raises the transport body limit ONLY for the workspace knowledge-file upload path
(POST /v1/workspaces/{id}/files) so an 8 MB file (WORKSPACE_FILE_MAX_BYTES, base64 ≈10.7 MB) is
not rejected at the gateway by the general ≤512 KB cap, while every other route — including the
workspace CRUD path /v1/workspaces/{id} and the per-file DELETE path
/v1/workspaces/{id}/files/{file_id} — keeps the general limit. /v1/chat/run keeps its own raised
attachment limit (ADR-020). These tests pin SizeLimitMiddleware._limit_for path-matching directly
(fast, exact) plus the config source-of-truth invariant. No I/O, no network, no LLM calls.
"""

from __future__ import annotations

import math
import uuid

import pytest

from app.api_gateway.middleware import SizeLimitMiddleware
from app.config import Settings


async def _noop_app(scope: object, receive: object, send: object) -> None:  # pragma: no cover
    # Minimal ASGI callable; SizeLimitMiddleware._limit_for never invokes downstream.
    return None


@pytest.fixture
def middleware() -> SizeLimitMiddleware:
    # __init__ reads get_settings() (lru_cache); the default Settings carry the ADR-045 defaults
    # (workspace_request_body_limit=12MB, size_limit_body=512KB, attachment limit=12MB).
    return SizeLimitMiddleware(_noop_app)


@pytest.fixture
def settings() -> Settings:
    return Settings()


_WID = str(uuid.uuid4())
_FID = str(uuid.uuid4())


# --- case 1: source-of-truth invariant on default settings (ADR-045 config.py) ---
def test_invariant_workspace_body_limit_covers_max_file_base64(settings: Settings) -> None:
    # INVARIANT (config.py ADR-045): workspace_request_body_limit must cover the base64-inflated
    # 8 MB file (×4/3 ≈10.67 MB) plus JSON-envelope slack. We assert the documented lower bound:
    # workspace_request_body_limit >= ceil(workspace_file_max_bytes * 4/3). The 12 MB default
    # (12_582_912 B) exceeds ceil(8 MB * 4/3) = 11_184_811 B, leaving >256 KB JSON slack.
    base64_inflated = math.ceil(settings.workspace_file_max_bytes * 4 / 3)
    assert settings.workspace_request_body_limit >= base64_inflated
    # Defaults are exactly the ADR-045 values (regression guard if an operator/code changes them).
    assert settings.workspace_file_max_bytes == 8 * 1024 * 1024
    assert settings.workspace_request_body_limit == 12 * 1024 * 1024
    # Documented slack recommendation (>=256 KB) holds for the defaults.
    assert settings.workspace_request_body_limit - base64_inflated >= 256 * 1024


# --- case 2: upload path gets the RAISED workspace limit ---
def test_limit_for_upload_path_returns_workspace_limit(
    middleware: SizeLimitMiddleware, settings: Settings
) -> None:
    path = f"/v1/workspaces/{_WID}/files"
    assert middleware._limit_for(path) == settings.workspace_request_body_limit
    # And that raised limit is NOT the general one (the whole point of ADR-045).
    assert middleware._limit_for(path) != settings.size_limit_body


# --- case 4: workspace CRUD path (no /files suffix) keeps the general limit ---
def test_limit_for_workspace_crud_path_returns_general_limit(
    middleware: SizeLimitMiddleware, settings: Settings
) -> None:
    assert middleware._limit_for(f"/v1/workspaces/{_WID}") == settings.size_limit_body


# --- case 5: per-file DELETE path (ends with file_id, NOT /files) keeps general limit ---
def test_limit_for_per_file_delete_path_returns_general_limit(
    middleware: SizeLimitMiddleware, settings: Settings
) -> None:
    path = f"/v1/workspaces/{_WID}/files/{_FID}"
    assert middleware._limit_for(path) == settings.size_limit_body
    assert middleware._limit_for(path) != settings.workspace_request_body_limit


# --- case 6: /v1/chat/run keeps its own attachment limit (ADR-020 unchanged) ---
def test_limit_for_chat_run_returns_attachment_limit(
    middleware: SizeLimitMiddleware, settings: Settings
) -> None:
    assert middleware._limit_for("/v1/chat/run") == settings.attachment_request_body_limit


# --- case 7: an unrelated route keeps the general limit ---
@pytest.mark.parametrize(
    "path",
    [
        "/v1/byok/set",
        "/v1/wallet/me",
        "/v1/policy/effective",
        "/v1/workspaces",  # collection path: no id, no /files suffix
        "/v1/workspaces/files",  # no id segment but DOES end with /files — still RAISED by design
    ],
)
def test_limit_for_other_paths_general_limit(
    middleware: SizeLimitMiddleware, settings: Settings, path: str
) -> None:
    if path == "/v1/workspaces/files":
        # Documented behavior: the rule matches prefix `/v1/workspaces/` + suffix `/files`,
        # so this degenerate path also gets the raised limit. It is not a real route (404 later),
        # and carries no security risk (still 413 above 12MB). Pin the actual behavior.
        assert middleware._limit_for(path) == settings.workspace_request_body_limit
    else:
        assert middleware._limit_for(path) == settings.size_limit_body
