"""Redis sliding-window rate limiting per user/device/IP (api-gateway/03, 05-security.md).

Uses a sorted-set sliding window. Limits from config (Q-003-1 defaults, TD-004). Source of
truth is Redis; on Redis unavailability we fail open with a logged warning (availability
over strictness on the rate-limit path).
"""

from __future__ import annotations

import logging
import time
import uuid

import redis.asyncio as redis

from app.config import get_settings
from app.observability.logging import log_event

logger = logging.getLogger("app.api_gateway.rate_limit")

_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(  # type: ignore[no-untyped-call]
            get_settings().redis_url, decode_responses=True
        )
    return _redis_client


async def _allow(client: redis.Redis, key: str, limit: int, window_seconds: int) -> bool:
    now = time.time()
    member = f"{now}:{uuid.uuid4()}"
    cutoff = now - window_seconds
    async with client.pipeline(transaction=True) as pipe:
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zadd(key, {member: now})
        pipe.zcard(key)
        pipe.expire(key, window_seconds)
        results = await pipe.execute()
    count = int(results[2])
    return count <= limit


async def enforce_chat_limits(*, user_id: uuid.UUID, device_id: str | None, ip: str | None) -> bool:
    """Returns True if allowed, False if any limit exceeded (→ 429)."""
    settings = get_settings()
    client = get_redis()
    window = settings.rate_limit_window_seconds
    checks: list[tuple[str, int]] = [
        (f"rl:user:{user_id}", settings.rate_limit_chat_per_user),
    ]
    if device_id:
        checks.append((f"rl:dev:{device_id}", settings.rate_limit_chat_per_device))
    if ip:
        checks.append((f"rl:ip:{ip}", settings.rate_limit_chat_per_ip))
    try:
        for key, limit in checks:
            if not await _allow(client, key, limit, window):
                return False
    except redis.RedisError as exc:
        log_event(logger, logging.WARNING, "rate_limit_redis_unavailable", error=str(exc))
        return True  # fail open
    return True


async def enforce_admin_limits(*, ip: str | None) -> bool:
    """Dedicated admin rate limit per source IP (ADR-009 §6). Isolated from user limits.

    Window is the shared rate_limit_window_seconds (60s by default); the per-minute cap is
    admin_rate_limit_per_min. When the client IP cannot be resolved (no trusted proxy / no peer),
    a single shared bucket is used so the admin surface is never left fully unlimited.
    """
    settings = get_settings()
    client = get_redis()
    bucket = ip or "unknown"
    try:
        return await _allow(
            client,
            f"rl:admin:{bucket}",
            settings.admin_rate_limit_per_min,
            settings.rate_limit_window_seconds,
        )
    except redis.RedisError as exc:
        log_event(logger, logging.WARNING, "rate_limit_redis_unavailable", error=str(exc))
        return True


async def enforce_auth_limits(*, ip: str | None) -> bool:
    """Per-IP rate limit on /v1/auth/* (ADR-018 §6). Auth endpoints are public (no JWT), so the
    only throttle is per source IP. When the client IP cannot be resolved, a single shared bucket
    is used so the surface is never fully unlimited. Window is the shared rate_limit_window_seconds.
    """
    settings = get_settings()
    client = get_redis()
    bucket = ip or "unknown"
    try:
        return await _allow(
            client,
            f"rl:auth:{bucket}",
            settings.auth_rate_limit_per_ip,
            settings.rate_limit_window_seconds,
        )
    except redis.RedisError as exc:
        log_event(logger, logging.WARNING, "rate_limit_redis_unavailable", error=str(exc))
        return True


async def enforce_cloudpayments_webhook_limits(*, ip: str | None) -> bool:
    """Per-source-IP rate limit on the PUBLIC CloudPayments webhook (ADR-054 §1).

    The endpoint is public (broadapps sends no auth), so the only throttle is per source IP; its
    purpose is anti-amplification of the outgoing verification GET, not blocking legitimate traffic
    (the cap is generous, default 120/min). When the client IP cannot be resolved a single shared
    ``unknown`` bucket is used so the surface is never fully unlimited. Fail-open on a Redis error
    (availability over strictness), like the other limiters. Window is rate_limit_window_seconds.
    """
    settings = get_settings()
    client = get_redis()
    bucket = ip or "unknown"
    try:
        return await _allow(
            client,
            f"rl:cpwebhook:{bucket}",
            settings.cloudpayments_webhook_rate_limit_per_ip,
            settings.rate_limit_window_seconds,
        )
    except redis.RedisError as exc:
        log_event(logger, logging.WARNING, "rate_limit_redis_unavailable", error=str(exc))
        return True


async def enforce_other_limits(*, user_id: uuid.UUID) -> bool:
    settings = get_settings()
    client = get_redis()
    try:
        return await _allow(
            client,
            f"rl:other:{user_id}",
            settings.rate_limit_other_per_user,
            settings.rate_limit_window_seconds,
        )
    except redis.RedisError as exc:
        log_event(logger, logging.WARNING, "rate_limit_redis_unavailable", error=str(exc))
        return True


async def redis_ping() -> bool:
    try:
        return bool(await get_redis().ping())
    except redis.RedisError:
        return False


async def close_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
    _redis_client = None
