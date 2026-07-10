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

from fastapi import APIRouter, Path, Query, Response
from sqlalchemy import and_, func, or_, select

from app.api_gateway.rate_limit import enforce_other_limits
from app.chat.image_cursor import ImageCursor, InvalidCursorError
from app.chat.image_sweep import maybe_sweep_expired_images
from app.deps import CurrentUser, DbSession
from app.errors import RateLimitedError, ValidationFailedError
from app.models import GeneratedImage
from app.schemas.images import ImageListItem, ImageListResponse

router = APIRouter(prefix="/v1/images", tags=["Images"])

_LIST_LIMIT_DEFAULT = 30
_LIST_LIMIT_MAX = 100

# No CSP sandbox (image is not active content); only nosniff + no-store (ADR-058 §2).
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
}


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


@router.get(
    "",
    response_model=ImageListResponse,
    summary="Список изображений",
    description=(
        "Список сгенерированных изображений владельца с курсорной пагинацией (галерея). Доступ "
        "только владельцу (`user_id == sub`); истёкшие по TTL временного чата в списке не видны. "
        "Порядок — новые первыми. Каждый элемент: `imageId`, `contentType`, `size`, `createdAt`, "
        "`sessionId` (`null` для временного чата) — без байтов; байты отдаёт "
        "`GET /v1/images/{imageId}`."
    ),
)
async def list_images(
    session: DbSession,
    current: CurrentUser,
    cursor: Annotated[str | None, Query(description="Курсор пагинации (opaque).")] = None,
    limit: Annotated[
        int, Query(ge=1, le=_LIST_LIMIT_MAX, description="Размер страницы (1..100).")
    ] = _LIST_LIMIT_DEFAULT,
) -> ImageListResponse:
    await _rate_limit(current.user_id)
    # Opportunistic best-effort sweep of expired rows (Redis-throttled, own tx, fail-open). Runs
    # before the list query and NEVER breaks it; list correctness does not depend on it (the
    # freshness condition below already hides expired rows) — same as GET /v1/images/{imageId}.
    await maybe_sweep_expired_images(session)

    decoded: ImageCursor | None = None
    if cursor:
        try:
            decoded = ImageCursor.decode(cursor)
        except InvalidCursorError as exc:
            raise ValidationFailedError("invalid cursor") from exc

    # Owner isolation + freshness in the SAME query (foreign/expired/missing simply absent).
    # Only metadata columns are selected — never content (BYTEA) or prompt.
    stmt = select(
        GeneratedImage.id,
        GeneratedImage.content_type,
        GeneratedImage.size,
        GeneratedImage.created_at,
        GeneratedImage.session_id,
    ).where(
        GeneratedImage.user_id == current.user_id,
        or_(
            GeneratedImage.expires_at.is_(None),
            GeneratedImage.expires_at > func.now(),
        ),
    )
    if decoded is not None:
        # Keyset over (created_at DESC, id DESC): rows strictly "after" the cursor.
        stmt = stmt.where(
            or_(
                GeneratedImage.created_at < decoded.created_at,
                and_(
                    GeneratedImage.created_at == decoded.created_at,
                    GeneratedImage.id < decoded.id,
                ),
            )
        )
    # Fetch limit+1 to compute next_cursor without a second count query.
    stmt = stmt.order_by(
        GeneratedImage.created_at.desc(),
        GeneratedImage.id.desc(),
    ).limit(limit + 1)

    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items = [
        ImageListItem(
            imageId=row.id,
            contentType=row.content_type,
            size=row.size,
            createdAt=row.created_at,
            sessionId=row.session_id,
        )
        for row in page_rows
    ]
    next_cursor: str | None = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = ImageCursor(created_at=last.created_at, id=last.id).encode()
    return ImageListResponse(items=items, nextCursor=next_cursor)


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
