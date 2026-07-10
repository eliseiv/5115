"""Integration: per-path transport body limit for workspace file upload (ADR-045).

Verifies the SizeLimitMiddleware wiring end-to-end through the ASGI app (gateway only — the
middleware reads the `content-length` header BEFORE parsing, so we do not need to actually push
12 MB through the socket; httpx sets content-length from the body we send):

  - a body just BELOW the workspace upload limit on POST /v1/workspaces/{id}/files is NOT rejected
    by the size-gate (no 413 on the transport layer — it reaches auth/validation, which may then
    return 401/422; the point is the gateway lets it through);
  - a body ABOVE the workspace upload limit on the SAME route is 413 with the documented
    {"error":{"code":"payload_too_large",...}} envelope;
  - the SAME oversized body on the workspace CRUD path /v1/workspaces/{id} (no /files suffix)
    keeps the general ≤512 KB limit -> 413.

Hermetic: real PostgreSQL container (via the shared `client` fixture), Anthropic/StoreKit faked at
the client boundary; no network, no LLM calls. Works with placeholder API keys.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from app.config import Settings
from tests.conftest import auth_headers

_SETTINGS = Settings()
_WORKSPACE_LIMIT = _SETTINGS.workspace_request_body_limit  # 12 MB
_GENERAL_LIMIT = _SETTINGS.size_limit_body  # 512 KB


@pytest.mark.asyncio
async def test_below_workspace_limit_not_rejected_by_size_gate(client: AsyncClient) -> None:
    # Content-Length just under the raised workspace limit (and well above the general 512 KB cap).
    # The size middleware must NOT 413 it. We send a forged content-length header so we do not
    # have to materialize ~11 MB; the body itself is small but the gate trusts content-length.
    uid = uuid.uuid4()
    cl = _WORKSPACE_LIMIT - 1024  # just below 12 MB, far above 512 KB
    r = await client.post(
        f"/v1/workspaces/{uuid.uuid4()}/files",
        content=b"{}",
        headers={
            **auth_headers(uid),
            "content-type": "application/json",
            "content-length": str(cl),
        },
    )
    # Passed the size-gate: anything but 413 (in practice 401/404/422 downstream). The ADR-045
    # claim is strictly "no transport 413 below the workspace limit on the upload path".
    assert r.status_code != 413, r.text


@pytest.mark.asyncio
async def test_above_workspace_limit_rejected_413(client: AsyncClient) -> None:
    # Content-Length above the raised workspace limit -> 413 with the payload_too_large envelope.
    uid = uuid.uuid4()
    cl = _WORKSPACE_LIMIT + 1
    r = await client.post(
        f"/v1/workspaces/{uuid.uuid4()}/files",
        content=b"{}",
        headers={
            **auth_headers(uid),
            "content-type": "application/json",
            "content-length": str(cl),
        },
    )
    assert r.status_code == 413, r.text
    body = r.json()
    assert body["error"]["code"] == "payload_too_large"
    assert "message" in body["error"]


@pytest.mark.asyncio
async def test_workspace_crud_path_keeps_general_limit_413(client: AsyncClient) -> None:
    # The workspace CRUD path (no /files suffix) keeps the general ≤512 KB limit: a body above it
    # but below the workspace upload limit must still be 413 (the raise is scoped to the upload
    # route only, ADR-045).
    uid = uuid.uuid4()
    cl = _GENERAL_LIMIT + 1  # > 512 KB, < 12 MB
    assert cl < _WORKSPACE_LIMIT
    r = await client.patch(
        f"/v1/workspaces/{uuid.uuid4()}",
        content=b"{}",
        headers={
            **auth_headers(uid),
            "content-type": "application/json",
            "content-length": str(cl),
        },
    )
    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "payload_too_large"
