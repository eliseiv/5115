"""Unit tests for the sliding-window rate limiter (api-gateway/03, fail-open behavior)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import redis.asyncio as aioredis

from app.api_gateway import rate_limit


class _FakePipeline:
    def __init__(self, store: dict[str, set[str]], key_counts: dict[str, int]) -> None:
        self._store = store
        self._key_counts = key_counts
        self._ops: list[tuple[str, Any]] = []

    def zremrangebyscore(self, key: str, *_a: Any) -> None:
        self._ops.append(("noop", None))

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self._key_counts[key] = self._key_counts.get(key, 0) + 1
        self._ops.append(("add", key))

    def zcard(self, key: str) -> None:
        self._ops.append(("card", key))

    def expire(self, key: str, _ttl: int) -> None:
        self._ops.append(("noop", None))

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for op, key in self._ops:
            if op == "card":
                results.append(self._key_counts.get(key, 0))
            else:
                results.append(None)
        return results

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None


class _FakeRedis:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline({}, self._counts)


class _BrokenRedis:
    def pipeline(self, transaction: bool = True) -> Any:
        raise aioredis.RedisError("down")


@pytest.fixture
def patch_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(rate_limit, "get_redis", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_allows_until_limit(patch_redis: _FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    uid = uuid.uuid4()
    # default per-user chat limit is 30; the 31st call exceeds.
    allowed = [
        await rate_limit.enforce_chat_limits(user_id=uid, device_id=None, ip=None)
        for _ in range(31)
    ]
    assert allowed[:30] == [True] * 30
    assert allowed[30] is False


@pytest.mark.asyncio
async def test_fails_open_when_redis_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rate_limit, "get_redis", lambda: _BrokenRedis())
    ok = await rate_limit.enforce_chat_limits(user_id=uuid.uuid4(), device_id="d", ip="1.2.3.4")
    assert ok is True  # availability over strictness
