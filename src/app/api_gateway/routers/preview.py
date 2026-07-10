"""Public preview route: GET /v1/preview/{projectId}/{token}/{path} (ADR-010, WB-7).

Serves a project's static files by signed URL. NO user JWT (authorization is the signed token).
Owner isolation, path-traversal guard, content-type from site_files, and sandbox security headers
per ADR-010 / website-builder/05-security.md. Errors:
- 403: invalid/expired signature OR projects.user_id != ownerUserId of the signature.
- 404: project or file not found (do not reveal existence of others' resources).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Response

from app.deps import DbSession
from app.observability.metrics import preview_request_total
from app.website.service import WebsiteService
from app.website.signed_url import PreviewSecretMissingError, verify_token

router = APIRouter(prefix="/v1/preview", tags=["Preview"])

# Sandbox headers for user-generated HTML/JS (ADR-010 threat model).
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "sandbox allow-scripts allow-forms; default-src 'self'; frame-ancestors 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Cache-Control": "private, no-store",
}


def _forbidden() -> Response:
    preview_request_total.labels(result="forbidden").inc()
    return Response(status_code=403, headers=dict(_SECURITY_HEADERS))


def _not_found() -> Response:
    preview_request_total.labels(result="not_found").inc()
    return Response(status_code=404, headers=dict(_SECURITY_HEADERS))


@router.get(
    "/{projectId}/{token}/{path:path}",
    summary="Превью сгенерированного сайта (signed URL)",
    description=(
        "Отдаёт файл проекта по подписанной ссылке. Без JWT — авторизация в подписи (HMAC+TTL). "
        "Изоляция владельца, защита от path-traversal, sandbox-заголовки безопасности."
    ),
    responses={
        200: {"description": "Содержимое файла с content-type из site_files."},
        403: {"description": "Невалидная/истёкшая подпись или несовпадение владельца."},
        404: {"description": "Проект или файл не найден."},
    },
)
async def get_preview(
    session: DbSession,
    project_id: Annotated[uuid.UUID, Path(alias="projectId")],
    token: Annotated[str, Path()],
    path: Annotated[str, Path()],
) -> Response:
    website = WebsiteService(session)
    project = await website.get_project_by_id(project_id)
    if project is None:
        # Do not reveal whether the project exists for others; 404 (a forged token to a
        # non-existent project would also land here).
        return _not_found()

    # Verify the signature binds this projectId AND the project's owner (constant-time + TTL).
    try:
        valid = verify_token(
            project_id=project_id,
            owner_user_id=project.user_id,
            token=token,
        )
    except PreviewSecretMissingError:
        return _forbidden()
    if not valid:
        return _forbidden()

    # Path-traversal guard + lookup by (project_id, normalized_path) — never the filesystem.
    file = await website.read_file(project_id=project_id, path=path)
    if file is None:
        return _not_found()

    preview_request_total.labels(result="ok").inc()
    headers = dict(_SECURITY_HEADERS)
    return Response(
        content=file.content,
        media_type=file.content_type,
        headers=headers,
    )
