"""Opportunistic sweep of expired generated_images (ADR-058 §6, best-effort — TD-036).

Physical deletion of expired image rows in small, throttled batches on a request path (the image
fetch ``GET /v1/images/{id}``). There is no scheduler/cron in the project (TD-010/TD-013), so GC is
driven by real image traffic. Privacy does NOT depend on this sweep: an expired image is already
logically unreachable by the ``expires_at`` condition in the fetch query (ADR-058 §2) — the sweep
only bounds table growth.

Single Redis key ``SET NX EX=interval`` combines THROTTLE and single-worker LOCK: only the worker
that sets the key in a given interval performs the batch delete; everyone else is a no-op.
Fail-open on Redis unavailability (WARNING log, request continues — same pattern as rate_limit.py).
Best-effort on a DB error (rollback + WARNING) so the caller's own query runs on a clean session.
"""

from __future__ import annotations

import logging

import redis.asyncio as redis
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_gateway.rate_limit import get_redis
from app.config import get_settings
from app.observability.logging import log_event

logger = logging.getLogger("app.chat.image_sweep")

_SWEEP_LOCK_KEY = "img:sweep:lock"


async def maybe_sweep_expired_images(session: AsyncSession) -> None:
    """Best-effort delete of one throttled batch of expired image rows (ADR-058 §6).

    Acquires the Redis throttle+lock key (``SET NX EX=IMAGE_SWEEP_MIN_INTERVAL_SECONDS``); on
    success deletes up to ``IMAGE_SWEEP_BATCH_SIZE`` rows with ``expires_at <= now()`` and commits.
    Redis error → fail-open (skip, WARNING). DB error → rollback + WARNING (never breaks caller).
    """
    settings = get_settings()
    client = get_redis()
    interval = max(settings.image_sweep_min_interval_seconds, 1)
    try:
        acquired = await client.set(_SWEEP_LOCK_KEY, "1", nx=True, ex=interval)
    except redis.RedisError as exc:
        log_event(logger, logging.WARNING, "image_sweep_redis_unavailable", error=str(exc))
        return
    if not acquired:
        # Throttled: another worker swept within the interval (or the key is still alive).
        return
    batch = max(settings.image_sweep_batch_size, 1)
    try:
        # DELETE ... LIMIT is not valid in PostgreSQL; bound the batch via a subquery on id.
        await session.execute(
            text(
                "DELETE FROM generated_images WHERE id IN ("
                "SELECT id FROM generated_images "
                "WHERE expires_at IS NOT NULL AND expires_at <= now() "
                "LIMIT :batch)"
            ),
            {"batch": batch},
        )
        await session.commit()
    except SQLAlchemyError as exc:
        # Best-effort: leave the (already logically-inaccessible) rows for a later sweep; keep the
        # session clean so the caller's fetch query still runs.
        await session.rollback()
        log_event(logger, logging.WARNING, "image_sweep_delete_failed", error=str(exc))
