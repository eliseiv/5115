"""Unit tests for the ADR-027 calendar.read contract alignment with calendar.create_events.

ADR-027 made calendar.read's range args consistent with calendar.create_events:
- arg names: start / end (was startDate / endDate) + optional calendarId;
- value format: ISO8601 datetime, naive local, no offset (e.g. '2026-06-11T09:00:00');
- both tools' TOOL_DESCRIPTIONS carry the IDENTICAL explicit format statement so the format
  reaches the model (not only the docs).

These assertions exercise tools.py directly (no app/DB): the catalog inputSchema, the strict
arg validation (extra='forbid' rejects the old startDate/endDate), the description parity, the
catalog count invariant (ADR-027 adds no tool) + client/non-mutating flags, and the Anthropic tool
definition schema.
The HTTP catalog wiring lives in tests/integration/test_tools_endpoint.py.
"""

from __future__ import annotations

import pytest

from app.chat.tools import (
    TOOL_DESCRIPTIONS,
    anthropic_tool_definitions,
    tool_catalog,
    validate_tool_args,
)

# The single ISO8601 datetime example that ADR-027 §4 mandates in BOTH calendar descriptions.
_ISO_EXAMPLE = "2026-06-11T09:00:00"


def _calendar_read_catalog_entry() -> dict:
    by_name = {t["name"]: t for t in tool_catalog()}
    return by_name["calendar.read"]


# --- Scenario 1: catalog inputSchema for calendar.read ---------------------------------------


def test_calendar_read_input_schema_has_start_end_calendarid() -> None:
    schema = _calendar_read_catalog_entry()["inputSchema"]
    props = schema["properties"]
    # New contract property names present.
    assert set(props.keys()) == {"start", "end", "calendarId"}, props.keys()
    # start/end are plain strings (ADR-027 §3: no server-side datetime validation).
    assert props["start"]["type"] == "string"
    assert props["end"]["type"] == "string"


def test_calendar_read_input_schema_requires_start_end_only() -> None:
    schema = _calendar_read_catalog_entry()["inputSchema"]
    # required is exactly [start, end]; calendarId is optional (start/end order-insensitive).
    assert set(schema.get("required", [])) == {"start", "end"}


def test_calendar_read_input_schema_has_no_legacy_date_args() -> None:
    schema = _calendar_read_catalog_entry()["inputSchema"]
    props = schema["properties"]
    # ADR-027 breaking rename: startDate / endDate must be gone entirely.
    assert "startDate" not in props
    assert "endDate" not in props


# --- Scenario 2: strict arg validation (extra='forbid') --------------------------------------


def test_validate_calendar_read_accepts_start_end() -> None:
    out = validate_tool_args(
        "calendar.read",
        {"start": "2026-06-11T09:00:00", "end": "2026-06-11T18:00:00"},
    )
    assert out["start"] == "2026-06-11T09:00:00"
    assert out["end"] == "2026-06-11T18:00:00"
    # calendarId defaults to None when omitted.
    assert out["calendarId"] is None


def test_validate_calendar_read_accepts_optional_calendar_id() -> None:
    out = validate_tool_args(
        "calendar.read",
        {"start": "2026-06-11T00:00:00", "end": "2026-06-12T00:00:00", "calendarId": "work"},
    )
    assert out["calendarId"] == "work"


def test_validate_calendar_read_rejects_legacy_date_args() -> None:
    # extra='forbid' on the strict model: the old startDate/endDate keys are now unknown fields.
    with pytest.raises(ValueError):
        validate_tool_args(
            "calendar.read",
            {"startDate": "2026-06-11", "endDate": "2026-06-11"},
        )


def test_validate_calendar_read_rejects_missing_required() -> None:
    with pytest.raises(ValueError):
        validate_tool_args("calendar.read", {"start": "2026-06-11T09:00:00"})


def test_validate_calendar_read_does_not_enforce_datetime_format() -> None:
    # ADR-027 §3: start/end stay plain str (no server-side datetime validation), symmetric with
    # CalendarEventInput. An arbitrary (non-datetime) string must pass pydantic untouched — the
    # format is communicated to the model via the description, not enforced by the backend.
    out = validate_tool_args(
        "calendar.read",
        {"start": "not-a-datetime", "end": "whatever"},
    )
    assert out["start"] == "not-a-datetime"
    assert out["end"] == "whatever"


# --- Scenario 3: description parity (identical format statement) ------------------------------


def test_calendar_read_description_states_iso_datetime_naive_local() -> None:
    desc = TOOL_DESCRIPTIONS["calendar.read"]
    assert "ISO8601 datetime" in desc
    assert _ISO_EXAMPLE in desc
    # naive local / no timezone offset must be spelled out for the model.
    assert "without timezone offset" in desc


def test_calendar_create_description_states_iso_datetime_naive_local() -> None:
    desc = TOOL_DESCRIPTIONS["calendar.create_events"]
    assert "ISO8601 datetime" in desc
    assert _ISO_EXAMPLE in desc
    assert "without timezone offset" in desc


def test_calendar_read_and_create_share_identical_format_clause() -> None:
    # ADR-027 §4: both descriptions must carry the SAME explicit format clause. We assert the
    # shared, load-bearing sentence fragment is byte-identical across the two tools.
    shared_clause = (
        f"ISO8601 datetime strings in local time without timezone offset, e.g. '{_ISO_EXAMPLE}'."
    )
    assert shared_clause in TOOL_DESCRIPTIONS["calendar.read"]
    assert shared_clause in TOOL_DESCRIPTIONS["calendar.create_events"]


# --- Scenario 4: catalog invariants (count + flags) ------------------------------------------


def test_catalog_tool_count_unchanged_by_adr027() -> None:
    # ADR-027 §6: only calendar.read's inputSchema changes; ADR-027 adds NO tool. The catalog count
    # is whatever the registry holds today (ADR-026 time.now + ADR-057 quiz.generate + ADR-058
    # image.generate = 16); the point of this assertion is that ADR-027 is inputSchema-only, so we
    # pin it to the current set (the catalog lists ALL tools unconditionally — image.generate is
    # present regardless of the ADR-058 key-gate, which constrains only the offered LLM set).
    assert len(tool_catalog()) == 16
    # quiz.generate (ADR-057) is present in the machine catalog (GET /v1/tools lists ALL tools; the
    # dialog-mode gate applies only to the offered LLM tool-set, not the /v1/tools catalog).
    by_name = {t["name"]: t for t in tool_catalog()}
    assert by_name["quiz.generate"]["execution"] == "server"


def test_calendar_read_is_client_side_and_non_mutating() -> None:
    entry = _calendar_read_catalog_entry()
    assert entry["execution"] == "client"
    assert entry["mutating"] is False


# --- Scenario 5: regression — other tools untouched ------------------------------------------


def test_other_tools_unchanged_by_adr027() -> None:
    # ADR-027 §6: only calendar.read's inputSchema changes. The set of tool NAMES and the schemas
    # of the unrelated tools must be byte-identical to their independent definition. We assert the
    # full name set and that calendar.create_events keeps its events[].start/end shape intact.
    names = {t["name"] for t in tool_catalog()}
    assert names == {
        "files.read",
        "files.write",
        "files.list",
        "files.mkdir",
        "calendar.read",
        "calendar.create_events",
        "reminders.read",
        "reminders.create",
        "site.write_file",
        "site.preview",
        "site.list",
        "site.read",
        "site.delete",
        "time.now",
        "quiz.generate",  # ADR-057: global server-side quiz tool (dialog-gated for the LLM only)
        "image.generate",  # ADR-058: global server-side image tool (key-gated for the LLM only)
    }


def test_calendar_create_events_schema_intact() -> None:
    # create_events range args live inside events[].start/end — ADR-027 must NOT alter create.
    by_name = {t["name"]: t for t in tool_catalog()}
    create_schema = by_name["calendar.create_events"]["inputSchema"]
    # Top-level is { events: [...] }; the per-event object carries start/end (not the old names).
    event_def = create_schema["$defs"]["CalendarEventInput"]
    event_props = event_def["properties"]
    assert "start" in event_props and "end" in event_props
    assert "startDate" not in event_props and "endDate" not in event_props


def test_calendar_create_is_client_side_and_mutating() -> None:
    by_name = {t["name"]: t for t in tool_catalog()}
    entry = by_name["calendar.create_events"]
    # Regression guard: create stays client-side + mutating (ADR-011), unaffected by ADR-027.
    assert entry["execution"] == "client"
    assert entry["mutating"] is True


# --- Scenario 6: Anthropic tool definition schema --------------------------------------------


def test_anthropic_calendar_read_input_schema_aligned() -> None:
    defs = {d["name"]: d for d in anthropic_tool_definitions()}
    # BUG-3 wire name for calendar.read is calendar_read (dot -> underscore).
    assert "calendar_read" in defs
    input_schema = defs["calendar_read"]["input_schema"]
    props = input_schema["properties"]
    assert set(props.keys()) == {"start", "end", "calendarId"}
    assert "startDate" not in props and "endDate" not in props
    assert set(input_schema.get("required", [])) == {"start", "end"}


def test_anthropic_calendar_read_definition_matches_catalog_schema() -> None:
    # The Anthropic input_schema and the GET /v1/tools inputSchema are generated from the SAME
    # Pydantic model (single source of truth) — they must be identical for calendar.read.
    defs = {d["name"]: d for d in anthropic_tool_definitions()}
    catalog_schema = _calendar_read_catalog_entry()["inputSchema"]
    assert defs["calendar_read"]["input_schema"] == catalog_schema
