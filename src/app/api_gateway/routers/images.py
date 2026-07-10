"""GET /v1/images/{imageId} — fetch generated image bytes under JWT (ADR-058 §2).

Serves the bytes of an image produced by the ``image.generate`` server-side tool. Authorization is
by ownership ONLY (``generated_images.user_id == sub``) — a foreign image → 404 (never reveal
existence of another user's resource, like preview / GET /v1/chats). An EXPIRED image (temporary
chat TTL, ADR-058 §6) → 404 too, enforced by an ``expires_at`` condition IN THE QUERY (logical
inaccessibility is unconditional, independent of physical GC — ADR-058 §2). Security headers:
``nosniff`` + ``no-store`` (no CSP sandbox — an image is not active content, unlike preview HTML).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Response
from sqlalchemy import func, or_, select

from app.chat.image_sweep import maybe_sweep_expired_images
from app.deps import CurrentUser, DbSession
from app.models import GeneratedImage

router = APIRouter(prefix="/v1/images", tags=["Images"])

# No CSP sandbox (image is not active content); only nosniff + no-store (ADR-058 §2).
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
}


@router.get(
    "/{imageId}",
    summary="Получить сгенерированное изображение",
    description=(
        "Отдаёт байты изображения, сгенерированного инструментом `image.generate`. Доступ только "
        "владельцу (`user_id == sub`); чужое ИЛИ истёкшее (TTL временного чата) → `404` (не "
        "раскрываем существование). Заголовки: `X-Content-Type-Options: nosniff`, "
        "`Cache-Control: private, no-store`."
    ),
    responses={
        200: {"description": "Байты изображения с его content-type."},
        404: {"description": "Изображение не найдено, чужое или истёкшее."},
    },
)
async def get_image(
    session: DbSession,
    current: CurrentUser,
    image_id: Annotated[uuid.UUID, Path(alias="imageId")],
) -> Response:
    # ADR-058 §6: opportunistic best-effort sweep of expired rows (Redis-throttled, own tx, fail-
    # open). Runs BEFORE the fetch so the fetch query sees a clean session; privacy does not depend
    # on it (the freshness condition below already hides expired rows).
    await maybe_sweep_expired_images(session)

    # ADR-058 §2: owner isolation + freshness in the SAME query. Foreign/expired/missing → 404.
    row = (
        await session.execute(
            select(GeneratedImage.content, GeneratedImage.content_type).where(
                GeneratedImage.id == image_id,
                GeneratedImage.user_id == current.user_id,
                or_(
                    GeneratedImage.expires_at.is_(None),
                    GeneratedImage.expires_at > func.now(),
                ),
            )
        )
    ).first()
    if row is None:
        return Response(status_code=404, headers=dict(_SECURITY_HEADERS))
    return Response(
        content=row.content,
        media_type=row.content_type,
        headers=dict(_SECURITY_HEADERS),
    )
