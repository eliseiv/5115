"""Service routes: /health, /healthz, /ready, /metrics (api-gateway/02, 07-deployment.md)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Response
from sqlalchemy import text

from app.api_gateway.rate_limit import redis_ping
from app.config import get_settings
from app.db import get_sessionmaker
from app.observability.metrics import render_metrics

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    summary="Liveness-проверка",
    description='Простая проверка, что процесс жив. JWT не требуется. Всегда `200 {status: "ok"}`.',
)
@router.get(
    "/healthz",
    summary="Liveness-проверка (алиас /health)",
    description=(
        "Алиас `GET /health` для healthcheck и smoke. Публичный, без JWT. "
        'Всегда `200 {status: "ok"}`.'
    ),
)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/ready",
    summary="Readiness-проверка",
    description=(
        "Проверяет готовность зависимостей (PostgreSQL, Redis). JWT не требуется. `200`, если "
        "обе доступны, иначе `503`. Тело: статус каждой зависимости."
    ),
)
async def ready(response: Response) -> dict[str, str]:
    db_ok = False
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001 - readiness probe reports, does not raise
        db_ok = False
    redis_ok = await redis_ping()
    if not (db_ok and redis_ok):
        response.status_code = 503
    return {"db": "ok" if db_ok else "down", "redis": "ok" if redis_ok else "down"}


@router.get(
    "/metrics",
    summary="Метрики Prometheus",
    description=(
        "Prometheus exposition для скрейпинга. JWT не требуется; защищён сетью и/или scrape-"
        "токеном (`X-Scrape-Token`). При неверном токене — `403`."
    ),
)
async def metrics(
    x_scrape_token: Annotated[str | None, Header()] = None,
) -> Response:
    settings = get_settings()
    if settings.metrics_scrape_token and x_scrape_token != settings.metrics_scrape_token:
        return Response(status_code=403)
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
