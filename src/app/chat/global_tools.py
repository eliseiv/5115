"""Global (project-independent) server-side tool handlers (ADR-026).

Unlike SiteToolHandlers (project-scoped site.*, ADR-011), these handlers are NOT tied to a
WebsiteService/project — they execute in the chat tool-loop without an external_project_id and
are offered to Claude in every turn (including «чистый чат» with no project, ADR-022).

Currently a single tool: ``time.now`` (ADR-026 §6). It returns the current date/time via an
injectable ``Clock`` provider (determinism for qa, ADR-026 §8 / 06-testing-strategy) — never a
direct ``datetime.now()``. The result always carries a UTC set (``utc``/``unix``/``weekday``);
a valid IANA ``tz`` additionally yields ``local``/``timezone``. An invalid/unknown/over-long tz
degrades to a ``ToolExecution.error("invalid_timezone", ...)`` (the turn survives, ADR-026 §6) —
never a raised exception.

The same ``ToolExecution`` contract as SiteToolHandlers is reused (single tool-result contract for
the orchestrator). Only the frozen dataclass is imported from website.tools — no website
infrastructure is instantiated here (ADR-026 §5).
"""

from __future__ import annotations

import datetime
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from app.chat.image_client import GeneratedImageData, ImageGenerationError, ImageGenerator
from app.chat.tools import (
    TIME_NOW_TZ_MAX_LENGTH,
    TOOL_QUIZ_GENERATE,
    TOOL_TIME_NOW,
    QuizGenerateArgs,
)
from app.website.tools import ToolExecution

# English weekday names by UTC date (Monday..Sunday), ADR-026 §6.
_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


@runtime_checkable
class Clock(Protocol):
    """Injectable source of the current time (ADR-026 §8).

    ``now()`` MUST return a timezone-aware UTC ``datetime``. The default implementation is
    ``SystemClock``; tests inject a ``FixedClock`` for determinism.
    """

    def now(self) -> datetime.datetime: ...


class SystemClock:
    """Default Clock: the real wall-clock time in UTC (ADR-026 §8)."""

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(tz=datetime.UTC)


class GlobalToolHandlers:
    """Dispatch + handlers for global server-side tools (ADR-026).

    Project-independent: no WebsiteService, no external_project_id, no session-context args. Time is
    taken from the injected ``Clock`` (default ``SystemClock``).

    ``image.generate`` (ADR-058) is NOT dispatched through ``execute``: it writes bytes and is
    tariffed, so the orchestrator drives it on a dedicated path (generate → debit → INSERT) and only
    borrows the injected ``ImageGenerator`` via ``generate_image``. The generator is injected here
    so the DI factory (deps.py) wires it in one place, mirroring the ``Clock`` injection.
    """

    def __init__(
        self, clock: Clock | None = None, image_generator: ImageGenerator | None = None
    ) -> None:
        self._clock = clock if clock is not None else SystemClock()
        self._image_generator = image_generator

    async def execute(self, *, tool_name: str, args: dict[str, Any]) -> ToolExecution:
        """Execute a global server-side tool. Returns a ToolExecution (result or error envelope)."""
        if tool_name == TOOL_TIME_NOW:
            return self._time_now(args)
        if tool_name == TOOL_QUIZ_GENERATE:
            return self._quiz_generate(args)
        # Unknown global tool name — should never happen (validated upstream against the registry).
        # image.generate never reaches here (orchestrator dedicated path).
        return ToolExecution.error("unknown_tool", f"unknown global server-side tool: {tool_name}")

    async def generate_image(
        self, *, prompt: str, size: str | None = None, quality: str | None = None
    ) -> GeneratedImageData:
        """Generate one image via the injected ``ImageGenerator`` (ADR-058 §3).

        Raises ``ImageContentPolicyError`` on a policy refusal and ``ImageGenerationError`` on any
        other failure — the orchestrator degrades both to a tool_result error (the turn survives).
        A missing generator (image tool not configured on this instance) is itself an
        ``ImageGenerationError`` so the turn degrades gracefully instead of crashing. The prompt is
        NOT logged here (TD-035).
        """
        if self._image_generator is None:
            raise ImageGenerationError("image generation is not configured on this instance")
        return await self._image_generator.generate(prompt=prompt, size=size, quality=quality)

    def _quiz_generate(self, args: dict[str, Any]) -> ToolExecution:
        """Execute quiz.generate (ADR-057 §2/§3/§4): validate the strict args + echo, else degrade.

        «Исполнение» of the quiz tool is pure: no external call, no mutation, no DB, no extra
        billing. This handler is the SOLE validator of the quiz invariants: valid args → echo the
        normalized dict as the tool_result (the orchestrator lifts it into ``ChatResponse.quiz``);
        INVALID args → ``ToolExecution.error("invalid_quiz", ...)`` — a tool_result error the model
        sees in the SAME turn and fixes by regenerating the quiz (graceful degrade, ADR-057 §3),
        exactly like ``time.now``'s ``invalid_timezone`` (ADR-026 §6). It never raises → the turn is
        never dropped with a 422; the ``tool_use``/``tool_result`` pair is preserved either way.

        Covers EVERY ``QuizGenerateArgs`` violation: options count outside 2..10, over-length
        ``question``/``option``/``explanation``, ``correctIndex`` outside ``[0, len(options))``, and
        any other schema failure — because the cleaned OpenAI-strict schema cannot express these
        invariants (ADR-057 §2), a violation by the model is expected, not an anomaly.

        TD-035 (learning-content redaction): the quiz text (``question``/``options``/
        ``explanation``) is user/learning content. It is NEVER logged nor written to an audit
        payload, and NEVER placed in the degrade message. The error reports WHICH field/constraint
        failed (field locus + machine error-type from pydantic, e.g. ``options:too_long``), never
        the submitted values — ``str(ValidationError)`` embeds ``input_value`` (the content), so it
        is deliberately NOT used. Only the tool name/status/short code leave this path.
        """
        try:
            validated = QuizGenerateArgs.model_validate(args).model_dump()
        except ValidationError as exc:
            # TD-035: build a content-free reason list from loc (field path) + type (error code)
            # only. Never touch err["input"]/err["msg"] (may carry the submitted learning content).
            reasons = sorted(
                {
                    f"{'.'.join(str(p) for p in err['loc']) or '<root>'}:{err['type']}"
                    for err in exc.errors()
                }
            )
            return ToolExecution.error(
                "invalid_quiz",
                "quiz arguments are invalid; regenerate the quiz within the limits "
                "(2-10 options; question<=1000, each option<=400, explanation<=2000 chars; "
                "0 <= correctIndex < number of options). Failed constraints: " + ", ".join(reasons),
            )
        return ToolExecution.ok(validated)

    def _time_now(self, args: dict[str, Any]) -> ToolExecution:
        now_utc = self._clock.now()
        # Defensive: a Clock contract violation (naive/non-UTC) would corrupt the offsets; normalize
        # to UTC so utc/unix/weekday are always correct (ADR-026 §6 — UTC set independent of tz).
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=datetime.UTC)
        else:
            now_utc = now_utc.astimezone(datetime.UTC)

        result: dict[str, Any] = {
            "utc": now_utc.isoformat(),
            # Integer Unix timestamp in seconds (UTC), ADR-026 §6.
            "unix": int(now_utc.timestamp()),
            "weekday": _WEEKDAYS[now_utc.weekday()],
        }

        tz_raw = args.get("tz")
        if tz_raw is None:
            # No tz → UTC-only set (timezone/local omitted), ADR-026 §6.
            return ToolExecution.ok(result)

        tz_name = str(tz_raw)
        # Q-026-1: length cap (≤ 64) enforced here so an over-long tz degrades to invalid_timezone
        # (a tool-result error, the turn survives) rather than 422-ing the turn (ADR-026 §6).
        if len(tz_name) > TIME_NOW_TZ_MAX_LENGTH:
            return ToolExecution.error(
                "invalid_timezone",
                f"timezone name exceeds the {TIME_NOW_TZ_MAX_LENGTH}-character limit",
            )
        try:
            zone = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            # Unknown/unparseable IANA name, missing tz database in the image (TD-019), or a
            # filesystem-hostile name when a tz database IS present (ZoneInfo treats the name as a
            # path and the OS rejects it, e.g. OSError Errno 22) → invalid_timezone tool-result
            # error; the UTC set is still available, the turn survives (ADR-026 §6).
            return ToolExecution.error(
                "invalid_timezone", f"unknown or unavailable timezone: {tz_name}"
            )

        local_dt = now_utc.astimezone(zone)
        # Normalized IANA name (key(zone) is the canonical name passed to ZoneInfo), ADR-026 §6.
        result["timezone"] = str(zone.key)
        result["local"] = local_dt.isoformat()
        return ToolExecution.ok(result)
