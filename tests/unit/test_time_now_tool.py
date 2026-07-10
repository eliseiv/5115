"""Unit tests for the global server-side tool ``time.now`` (ADR-026).

Determinism contract (ADR-026 §8 / 06-testing-strategy.md §time.now): ``time.now`` reads the
current time through an injectable ``Clock`` (timezone-aware UTC). Tests inject a ``FixedClock``
so the full JSON shape is deterministic — never a direct ``datetime.now()``.

UTC set (``utc``/``unix``/``weekday``) needs no tz database. The ``tz``→``local``/``timezone`` path
needs a tz base in the environment (TD-019). When the slim test env lacks ``tzdata`` (the case on
the CI runner — see 06-testing-strategy.md note), ``ZoneInfo('Europe/Moscow')`` raises and the
tz-positive case is skipped here (it is covered explicitly via ``uv run --with tzdata pytest``);
the invalid-tz / over-long-tz / forbid cases stay green regardless of tzdata.
"""

from __future__ import annotations

import datetime

import pytest

from app.chat.global_tools import Clock, GlobalToolHandlers, SystemClock
from app.chat.tools import TIME_NOW_TZ_MAX_LENGTH, TOOL_TIME_NOW
from app.website.tools import ToolExecution

# A fixed instant with sub-second precision: 2026-06-10T14:23:05.123456+00:00 (a Wednesday).
_FIXED_DT = datetime.datetime(2026, 6, 10, 14, 23, 5, 123456, tzinfo=datetime.UTC)
_FIXED_UNIX = int(_FIXED_DT.timestamp())


class FixedClock:
    """Deterministic Clock returning a pinned UTC instant (ADR-026 §8)."""

    def __init__(self, fixed_dt: datetime.datetime) -> None:
        self._fixed_dt = fixed_dt

    def now(self) -> datetime.datetime:
        return self._fixed_dt


def _handlers(fixed_dt: datetime.datetime = _FIXED_DT) -> GlobalToolHandlers:
    return GlobalToolHandlers(clock=FixedClock(fixed_dt))


def _tzdata_available(name: str = "Europe/Moscow") -> bool:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


# ----------------------------- 1. UTC-only set (no tz) -----------------------------
@pytest.mark.asyncio
async def test_time_now_without_tz_returns_exact_utc_set() -> None:
    execution = await _handlers().execute(tool_name=TOOL_TIME_NOW, args={})

    assert isinstance(execution, ToolExecution)
    assert execution.is_error is False
    result = execution.result
    assert result is not None
    # Exact JSON shape per ADR-026 §6: utc (ISO8601 +00:00), integer unix, English weekday.
    assert result == {
        "utc": "2026-06-10T14:23:05.123456+00:00",
        "unix": _FIXED_UNIX,
        "weekday": "Wednesday",
    }
    # local / timezone are OMITTED when no tz is supplied.
    assert "local" not in result
    assert "timezone" not in result


@pytest.mark.asyncio
async def test_time_now_explicit_tz_none_is_utc_only() -> None:
    # tz explicitly null behaves identically to omitting it (ADR-026 §6: default UTC).
    execution = await _handlers().execute(tool_name=TOOL_TIME_NOW, args={"tz": None})
    assert execution.result is not None
    assert set(execution.result) == {"utc", "unix", "weekday"}


@pytest.mark.asyncio
async def test_time_now_weekday_tracks_fixed_date() -> None:
    # A different fixed date → different weekday, proving weekday derives from the clock, not "now".
    sunday = datetime.datetime(2026, 6, 14, 0, 0, 0, tzinfo=datetime.UTC)
    execution = await _handlers(sunday).execute(tool_name=TOOL_TIME_NOW, args={})
    assert execution.result is not None
    assert execution.result["weekday"] == "Sunday"


# ----------------------------- 2. valid tz adds local/timezone -----------------------------
@pytest.mark.asyncio
async def test_time_now_with_valid_tz_adds_local_and_timezone() -> None:
    if not _tzdata_available("Europe/Moscow"):
        pytest.skip(
            "tz database unavailable in this environment (TD-019); run with "
            "`uv run --with tzdata pytest` to exercise the tz-positive path."
        )
    execution = await _handlers().execute(tool_name=TOOL_TIME_NOW, args={"tz": "Europe/Moscow"})
    assert execution.is_error is False
    result = execution.result
    assert result is not None
    # UTC set unchanged (driven by fixed_dt), independent of tz.
    assert result["utc"] == "2026-06-10T14:23:05.123456+00:00"
    assert result["unix"] == _FIXED_UNIX
    assert result["weekday"] == "Wednesday"
    # Local set present: normalized IANA name + ISO8601 with the zone offset (+03:00 for Moscow).
    assert result["timezone"] == "Europe/Moscow"
    assert result["local"] == "2026-06-10T17:23:05.123456+03:00"
    assert set(result) == {"utc", "unix", "weekday", "timezone", "local"}


# ----------------------------- 3. invalid / unknown tz -----------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_tz",
    ["Mars/Phobos", "not a zone", "Europe/Atlantis", "a\x00b", "..", "foo/../bar"],
)
async def test_time_now_invalid_tz_degrades_to_error_envelope(bad_tz: str) -> None:
    execution = await _handlers().execute(tool_name=TOOL_TIME_NOW, args={"tz": bad_tz})
    # Tool-result error, NOT a raised exception — the turn survives (ADR-026 §6).
    assert execution.is_error is True
    assert execution.error_code == "invalid_timezone"
    assert execution.result is None
    payload = execution.to_tool_result_payload()
    assert payload["error"]["code"] == "invalid_timezone"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_tz", ["***", "a*b", "a?b", "a|b"])
async def test_time_now_garbage_tz_with_fs_hostile_chars_must_not_crash_turn(bad_tz: str) -> None:
    """ADR-026 §6: ANY invalid/garbage tz must degrade to invalid_timezone, never raise.

    A garbage name with filesystem-hostile characters (e.g. ``***`` / ``a*b``) is the «мусор» case
    from 06-testing-strategy.md. When a tz database IS present (TD-019 target prod state,
    ADR-026 §10), ``ZoneInfo('a*b')`` resolves through importlib.resources / the tzpath and the OS
    rejects the name as a path → it raises ``OSError`` (e.g. Errno 22 EINVAL), NOT
    ZoneInfoNotFoundError/ValueError. The original handler's narrow ``except (ZoneInfoNotFoundError,
    ValueError)`` did NOT catch ``OSError`` → it propagated and crashed the turn (the regression).
    The fix widens the guard to ``except (ZoneInfoNotFoundError, ValueError, OSError)`` so the turn
    degrades to ``invalid_timezone`` even with a tz base present.

    REGRESSION SCOPE: under a tz-LESS environment (the bare CI gate has no ``tzdata``)
    ``ZoneInfo('a*b')`` raises ``ZoneInfoNotFoundError`` and this passed even on the OLD code; the
    OSError defect manifests ONLY with a tz base present — i.e. it is exercised by
    ``uv run --with tzdata pytest`` (and prod after TD-019). This test is the explicit guard for
    that path: green on the new code under tzdata, would crash on the old narrow-except.
    """
    execution = await _handlers().execute(tool_name=TOOL_TIME_NOW, args={"tz": bad_tz})
    assert execution.is_error is True
    assert execution.error_code == "invalid_timezone"
    assert execution.result is None


# ----------------------------- 4. over-long tz (Q-026-1, ≤ 64) -----------------------------
@pytest.mark.asyncio
async def test_time_now_over_long_tz_is_invalid_timezone_before_resolve() -> None:
    over_long = "A" * (TIME_NOW_TZ_MAX_LENGTH + 1)
    execution = await _handlers().execute(tool_name=TOOL_TIME_NOW, args={"tz": over_long})
    assert execution.is_error is True
    assert execution.error_code == "invalid_timezone"


@pytest.mark.asyncio
async def test_time_now_tz_at_length_limit_is_resolved_not_length_rejected() -> None:
    # Exactly at the limit is NOT rejected by the length guard; it fails (if at all) only on
    # zoneinfo resolution → still invalid_timezone (the 64-char name is not a real IANA zone).
    at_limit = "A" * TIME_NOW_TZ_MAX_LENGTH
    assert len(at_limit) == TIME_NOW_TZ_MAX_LENGTH
    execution = await _handlers().execute(tool_name=TOOL_TIME_NOW, args={"tz": at_limit})
    assert execution.is_error is True
    assert execution.error_code == "invalid_timezone"


# ----------------------------- 5. args extra=forbid (schema) -----------------------------
def test_time_now_args_forbid_extra_keys() -> None:
    from app.chat.tools import validate_tool_args

    with pytest.raises(ValueError):
        validate_tool_args(TOOL_TIME_NOW, {"tz": "UTC", "unexpected": 1})


def test_time_now_args_accept_empty_and_tz_only() -> None:
    from app.chat.tools import validate_tool_args

    assert validate_tool_args(TOOL_TIME_NOW, {}) == {"tz": None}
    assert validate_tool_args(TOOL_TIME_NOW, {"tz": "UTC"}) == {"tz": "UTC"}


# ----------------------------- 6. determinism + SystemClock default -----------------------------
@pytest.mark.asyncio
async def test_fixed_clock_is_repeatable() -> None:
    handlers = _handlers()
    first = await handlers.execute(tool_name=TOOL_TIME_NOW, args={})
    second = await handlers.execute(tool_name=TOOL_TIME_NOW, args={})
    assert first.result == second.result


@pytest.mark.asyncio
async def test_default_clock_is_system_clock_and_returns_current_utc() -> None:
    # No clock injected → SystemClock; result is the real, UTC-aware, near-now time.
    before = datetime.datetime.now(tz=datetime.UTC)
    execution = await GlobalToolHandlers().execute(tool_name=TOOL_TIME_NOW, args={})
    after = datetime.datetime.now(tz=datetime.UTC)
    assert execution.result is not None
    produced = datetime.datetime.fromisoformat(execution.result["utc"])
    assert produced.tzinfo is not None
    assert produced.utcoffset() == datetime.timedelta(0)  # UTC offset
    assert before <= produced <= after


def test_system_clock_satisfies_clock_protocol() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FixedClock(_FIXED_DT), Clock)


@pytest.mark.asyncio
async def test_naive_or_non_utc_clock_is_normalized_to_utc() -> None:
    # Defensive: a Clock contract violation (naive datetime) must not corrupt offsets — the handler
    # normalizes to UTC so utc/unix/weekday stay correct (global_tools._time_now normalization).
    naive = datetime.datetime(2026, 6, 10, 14, 23, 5, 123456)  # no tzinfo
    execution = await _handlers(naive).execute(tool_name=TOOL_TIME_NOW, args={})
    assert execution.result is not None
    assert execution.result["utc"] == "2026-06-10T14:23:05.123456+00:00"
    assert execution.result["unix"] == _FIXED_UNIX


# ----------------------------- dispatch: unknown global tool -----------------------------
@pytest.mark.asyncio
async def test_execute_unknown_global_tool_returns_error_envelope() -> None:
    execution = await _handlers().execute(tool_name="time.tomorrow", args={})
    assert execution.is_error is True
    assert execution.error_code == "unknown_tool"
