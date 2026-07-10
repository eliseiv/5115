"""Tool schemas (CO-1): client-side iOS tools + server-side site.* tools, Pydantic v2.

Two classes (ADR-011):
- client-side (files.*/calendar.*/reminders.*): backend only INITIATES the tool-call; the iOS
  client executes it and posts a tool_result.
- server-side (site.*): backend EXECUTES the handler itself, in the same tool-loop, without a
  round-trip to iOS (SERVER_SIDE_TOOLS).

Mutating tools (files.write, files.mkdir, calendar.create_events, reminders.create,
site.write_file, site.delete) require an audit record. Args/result are strictly validated
(extra='forbid'); `path` rejects `..`-traversal.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import IMAGE_QUALITY_VALUES, IMAGE_SIZE_VALUES, get_settings

# Tool names (fixed list — validated at the API boundary).
TOOL_FILES_READ = "files.read"
TOOL_FILES_WRITE = "files.write"
TOOL_FILES_LIST = "files.list"
TOOL_FILES_MKDIR = "files.mkdir"
TOOL_CALENDAR_READ = "calendar.read"
TOOL_CALENDAR_CREATE = "calendar.create_events"
TOOL_REMINDERS_READ = "reminders.read"
TOOL_REMINDERS_CREATE = "reminders.create"

# Server-side tools (site.*, ADR-011): executed by the backend, not the iOS client.
TOOL_SITE_WRITE_FILE = "site.write_file"
TOOL_SITE_PREVIEW = "site.preview"
TOOL_SITE_LIST = "site.list"
TOOL_SITE_READ = "site.read"
TOOL_SITE_DELETE = "site.delete"

# Global server-side tool (time.now, ADR-026): executed by the backend, like site.*, but
# project-INDEPENDENT — offered to Claude ALWAYS (including «чистый чат» with no project) and
# routed before the project-scoped branch (no external_project_id, no has_project guard).
TOOL_TIME_NOW = "time.now"

# Global server-side tool (quiz.generate, ADR-057): project-INDEPENDENT like time.now, but
# offered to the model ONLY when the session's dialog mode is study_learn (dialog-mode gate below).
# «Исполнение» = validate the strict args + echo the dict back as the tool_result (no external
# call, no mutation, no extra billing). Its result is surfaced in ChatResponse.quiz.
TOOL_QUIZ_GENERATE = "quiz.generate"

# ADR-055/ADR-057: dialog mode that unlocks quiz.generate. Kept here as the SSOT for the tool gate
# (the orchestrator resolves/validates the session dialog mode; this constant only names the value).
DIALOG_MODE_STUDY_LEARN = "study_learn"

# Global server-side tool (image.generate, ADR-058): project-INDEPENDENT like time.now, offered in
# every dialog mode (NOT dialog-mode-gated). UNLIKE the pure echo tools (time.now/quiz.generate) it
# WRITES bytes (generated_images) and is TARIFFED (a separate IMAGE_CREDITS_COST debit on top of the
# turn) — so it is BOTH a GLOBAL_SERVER_SIDE_TOOL and a MUTATING_TOOL. The orchestrator handles it
# with a dedicated path (generate bytes → debit → INSERT), NOT the generic echo dispatch.
TOOL_IMAGE_GENERATE = "image.generate"

# Project-scoped server-side tools (site.*, ADR-011/022): executed by the backend in the
# tool-loop; offered to Claude ONLY when the session has a project (project_id IS NOT NULL).
SERVER_SIDE_TOOLS = frozenset(
    {
        TOOL_SITE_WRITE_FILE,
        TOOL_SITE_PREVIEW,
        TOOL_SITE_LIST,
        TOOL_SITE_READ,
        TOOL_SITE_DELETE,
    }
)

# Global (project-independent) server-side tools (ADR-026 §2). DISJOINT from SERVER_SIDE_TOOLS:
# the two registries are mutually exclusive (invariant GLOBAL_SERVER_SIDE_TOOLS ∩ SERVER_SIDE_TOOLS
# = ∅). Combined server-side = SERVER_SIDE_TOOLS ∪ GLOBAL_SERVER_SIDE_TOOLS; everything else in
# ALL_TOOL_NAMES is client-side.
GLOBAL_SERVER_SIDE_TOOLS = frozenset({TOOL_TIME_NOW, TOOL_QUIZ_GENERATE, TOOL_IMAGE_GENERATE})

ALL_TOOL_NAMES = frozenset(
    {
        TOOL_FILES_READ,
        TOOL_FILES_WRITE,
        TOOL_FILES_LIST,
        TOOL_FILES_MKDIR,
        TOOL_CALENDAR_READ,
        TOOL_CALENDAR_CREATE,
        TOOL_REMINDERS_READ,
        TOOL_REMINDERS_CREATE,
        *SERVER_SIDE_TOOLS,
        *GLOBAL_SERVER_SIDE_TOOLS,
    }
)

# BUG-3: Anthropic Messages API requires tool.name to match ^[a-zA-Z0-9_-]{1,128}$ — a dot is
# rejected with 400 (→ backend 502). The public iOS contract (TZ §5) uses dotted domain names and
# must NOT change. We therefore keep a static, bidirectional name map (13 fixed pairs, incl.
# server-side site.*) that is the single source of truth for name correspondence. It is applied
# ONLY at the Anthropic transport
# boundary: forward (domain→anthropic) when building tools[].name for messages.create, reverse
# (anthropic→domain) when parsing a tool_use block from Claude. Everywhere else — DB
# (tool_calls.tool_name), audit, API responses (toolCall.name), arg/result typing — stays domain.
_DOMAIN_TO_ANTHROPIC: dict[str, str] = {
    TOOL_FILES_READ: "files_read",
    TOOL_FILES_WRITE: "files_write",
    TOOL_FILES_LIST: "files_list",
    TOOL_FILES_MKDIR: "files_mkdir",
    TOOL_CALENDAR_READ: "calendar_read",
    TOOL_CALENDAR_CREATE: "calendar_create_events",
    TOOL_REMINDERS_READ: "reminders_read",
    TOOL_REMINDERS_CREATE: "reminders_create",
    # Server-side site.* (ADR-011 §3): same dot→underscore mapping as client-side tools.
    TOOL_SITE_WRITE_FILE: "site_write_file",
    TOOL_SITE_PREVIEW: "site_preview",
    TOOL_SITE_LIST: "site_list",
    TOOL_SITE_READ: "site_read",
    TOOL_SITE_DELETE: "site_delete",
    # Global server-side time.now (ADR-026 §2): same dot→underscore mapping.
    TOOL_TIME_NOW: "time_now",
    # Global server-side quiz.generate (ADR-057 §4): same dot→underscore mapping.
    TOOL_QUIZ_GENERATE: "quiz_generate",
    # Global server-side image.generate (ADR-058 §3): same dot→underscore mapping.
    TOOL_IMAGE_GENERATE: "image_generate",
}
_ANTHROPIC_TO_DOMAIN: dict[str, str] = {a: d for d, a in _DOMAIN_TO_ANTHROPIC.items()}


class UnknownToolNameError(Exception):
    """Claude returned a tool_use.name that is not in the static map (upstream anomaly).

    Treated as an upstream processing error, never forwarded to iOS as a valid tool name.
    """


def to_anthropic_tool_name(domain_name: str) -> str:
    """Forward map domain-name (dotted) → anthropic-name (underscore). Static table only."""
    anthropic_name = _DOMAIN_TO_ANTHROPIC.get(domain_name)
    if anthropic_name is None:
        raise UnknownToolNameError(f"unknown domain tool name: {domain_name}")
    return anthropic_name


def to_domain_tool_name(anthropic_name: str) -> str:
    """Reverse map anthropic-name (underscore) → domain-name (dotted). Static table only.

    Raises UnknownToolNameError if Claude returns a name absent from the map (upstream anomaly).
    """
    domain_name = _ANTHROPIC_TO_DOMAIN.get(anthropic_name)
    if domain_name is None:
        raise UnknownToolNameError(f"unknown anthropic tool name: {anthropic_name}")
    return domain_name


# Mutating tools require audit (AC-7; ADR-011 §4 adds site.write_file / site.delete).
MUTATING_TOOLS = frozenset(
    {
        TOOL_FILES_WRITE,
        TOOL_FILES_MKDIR,
        TOOL_CALENDAR_CREATE,
        TOOL_REMINDERS_CREATE,
        TOOL_SITE_WRITE_FILE,
        TOOL_SITE_DELETE,
        # ADR-058 §1/§4: image.generate writes bytes (generated_images) AND is tariffed — a real
        # mutation, unlike the echo-only time.now/quiz.generate. In MUTATING_TOOLS for the catalog
        # (mutating: true); its own audit trail is the tool_call_completed/billing_debit events, not
        # a tool_mutation record (the orchestrator handles it on a dedicated path).
        TOOL_IMAGE_GENERATE,
    }
)


def _validate_safe_path(value: str) -> str:
    parts = value.replace("\\", "/").split("/")
    if ".." in parts:
        raise ValueError("path must not contain '..' traversal")
    return value


SafePath = Annotated[str, Field(min_length=1)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PathModel(_StrictModel):
    path: SafePath

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return _validate_safe_path(value)


# --- files ---
class FilesReadArgs(_PathModel):
    pass


class FilesWriteArgs(_PathModel):
    content: str
    encoding: Literal["utf8", "base64"]
    overwrite: bool


class FilesListArgs(_PathModel):
    recursive: bool


class FilesMkdirArgs(_PathModel):
    createIntermediates: bool


# --- calendar ---
class CalendarReadArgs(_StrictModel):
    start: str
    end: str
    calendarId: str | None = None


class CalendarEventInput(_StrictModel):
    title: str
    start: str
    end: str
    location: str | None = None
    notes: str | None = None
    calendarId: str | None = None


class CalendarCreateArgs(_StrictModel):
    events: list[CalendarEventInput]


# --- reminders ---
class RemindersReadArgs(_StrictModel):
    listId: str | None = None
    includeCompleted: bool


class ReminderInput(_StrictModel):
    title: str
    due: str | None = None
    notes: str | None = None
    listId: str | None = None


class RemindersCreateArgs(_StrictModel):
    reminders: list[ReminderInput]


# --- server-side site.* (ADR-011) ---
# IMPORTANT (IDOR guard, website-builder/05-security.md): args carry ONLY file data. The owning
# userId and external_project_id come from the session context on the backend, NEVER from these
# args — so the model cannot target another user's project.
class SiteWriteFileArgs(_PathModel):
    content: str
    contentType: str
    encoding: Literal["utf8", "base64"]


class SitePreviewArgs(_StrictModel):
    entry: str | None = None


class SiteListArgs(_StrictModel):
    pass


class SiteReadArgs(_PathModel):
    pass


class SiteDeleteArgs(_PathModel):
    pass


# --- global server-side time.now (ADR-026) ---
# Q-026-1: length cap for the optional tz arg (≤ 64 — longer than any valid IANA name). Enforced
# in the handler (GlobalToolHandlers) so an over-limit tz becomes a tool-result error
# `invalid_timezone` (the turn survives, ADR-026 §6) rather than a 422 of the turn. It is therefore
# NOT a pydantic max_length constraint here (that would 422 the turn instead).
TIME_NOW_TZ_MAX_LENGTH = 64


class TimeNowArgs(_StrictModel):
    """Args for time.now (ADR-026 §6): optional IANA timezone name (e.g. Europe/Moscow).

    `extra='forbid'` (any other key → args validation error, like other tools). `tz` length and
    IANA validity are checked in GlobalToolHandlers, not here — an invalid/over-long tz must degrade
    to a tool-result error `invalid_timezone`, not fail the turn with 422 (Q-026-1, ADR-026 §6).
    """

    tz: str | None = None


# --- global server-side quiz.generate (ADR-057) ---
# Length caps for the strict quiz schema. All fields are REQUIRED — OpenAI strict-mode forbids
# optional properties (strict requires every property in `required`); `extra='forbid'` gives
# `additionalProperties: false` (also a strict requirement). The caps/counts are emitted into the
# JSON schema handed to OpenAI (tool_input_schema); the cross-field `correctIndex < len(options)`
# rule is a model_validator (JSON Schema cannot express it, so it is checked in Python — an
# out-of-range index is rejected via validate_tool_args, like any other bad tool args, ADR-057 §3).
QUIZ_QUESTION_MAX_LENGTH = 1000
QUIZ_OPTION_MAX_LENGTH = 400
QUIZ_EXPLANATION_MAX_LENGTH = 2000
QUIZ_MIN_OPTIONS = 2
QUIZ_MAX_OPTIONS = 10


class QuizGenerateArgs(_StrictModel):
    """Args for quiz.generate (ADR-057 §3): one Study & Learn quiz card, strict schema.

    All fields required (OpenAI strict-mode: no optional properties). `question`/`explanation` are
    length-capped; `options` carries 2..10 variants (each length-capped); `correctIndex` is the
    0-based index of the correct variant. The `correctIndex < len(options)` invariant is enforced by
    the model_validator below (not expressible in JSON Schema) — an out-of-range index fails
    validation and the quiz tool_use is rejected (ADR-057 §3). camelCase field names match the rest
    of the public contract (e.g. `correctIndex`).
    """

    question: Annotated[str, Field(min_length=1, max_length=QUIZ_QUESTION_MAX_LENGTH)]
    options: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=QUIZ_OPTION_MAX_LENGTH)]],
        Field(min_length=QUIZ_MIN_OPTIONS, max_length=QUIZ_MAX_OPTIONS),
    ]
    correctIndex: int
    explanation: Annotated[str, Field(min_length=1, max_length=QUIZ_EXPLANATION_MAX_LENGTH)]

    @field_validator("correctIndex", mode="before")
    @classmethod
    def _reject_bool_index(cls, value: Any) -> Any:
        # bool is an int subclass in Python, and pydantic coerces bool→int BEFORE any mode="after"
        # validator runs — so this check MUST be mode="before" (on the raw input) to actually fire.
        # A JSON `true`/`false` for an index is nonsense; reject it instead of silently accepting
        # 1/0 (ADR-057 §3). The raised ValueError becomes a pydantic ValidationError → the degrade
        # path (_quiz_generate) turns it into a content-free `invalid_quiz` tool_result, not a 422.
        if isinstance(value, bool):
            raise ValueError("correctIndex must be an integer, not a boolean")
        return value

    @field_validator("correctIndex")
    @classmethod
    def _non_negative_index(cls, value: int) -> int:
        # Lower bound (mode="after", on the coerced int). Upper bound needs len(options) → the
        # model_validator below. A negative index is out of range for any options list.
        if value < 0:
            raise ValueError("correctIndex must be non-negative")
        return value

    @model_validator(mode="after")
    def _index_in_range(self) -> QuizGenerateArgs:
        if self.correctIndex >= len(self.options):
            raise ValueError("correctIndex must be within the options range")
        return self


# --- global server-side image.generate (ADR-058) ---
# Prompt length cap. Enforced as a pydantic constraint: an over-long prompt is a hard tool-args
# validation error (like any non-quiz tool), NOT a graceful degrade — the model controls the prompt
# and is told the limit in the tool description.
IMAGE_PROMPT_MAX_LENGTH = 4000


class ImageGenerateArgs(_StrictModel):
    """Args for image.generate (ADR-058 §3): a prompt + optional size/quality.

    ``prompt`` is required and length-capped. ``size``/``quality`` are OPTIONAL (``None`` → the
    instance default resolved from config); when provided they must be one of the allowed
    gpt-image-1 values (``IMAGE_SIZE_VALUES`` / ``IMAGE_QUALITY_VALUES``, the SAME SSOT the config
    defaults use), else a tool-args validation error. NOT a strict OpenAI tool (``image.generate``
    is deliberately
    absent from ``_OPENAI_STRICT_TOOLS``): the optional fields serialize to ``anyOf`` (which strict
    would need the allowlist-clean to survive) and the values are already enforced here by pydantic,
    so strict adds nothing — the schema is sent UNCHANGED. ``extra='forbid'`` rejects unknown keys.
    """

    prompt: Annotated[str, Field(min_length=1, max_length=IMAGE_PROMPT_MAX_LENGTH)]
    size: str | None = None
    quality: str | None = None

    @field_validator("size")
    @classmethod
    def _check_size(cls, value: str | None) -> str | None:
        if value is not None and value not in IMAGE_SIZE_VALUES:
            raise ValueError(f"size must be one of {sorted(IMAGE_SIZE_VALUES)}")
        return value

    @field_validator("quality")
    @classmethod
    def _check_quality(cls, value: str | None) -> str | None:
        if value is not None and value not in IMAGE_QUALITY_VALUES:
            raise ValueError(f"quality must be one of {sorted(IMAGE_QUALITY_VALUES)}")
        return value


_ARGS_BY_TOOL: dict[str, type[_StrictModel]] = {
    TOOL_FILES_READ: FilesReadArgs,
    TOOL_FILES_WRITE: FilesWriteArgs,
    TOOL_FILES_LIST: FilesListArgs,
    TOOL_FILES_MKDIR: FilesMkdirArgs,
    TOOL_CALENDAR_READ: CalendarReadArgs,
    TOOL_CALENDAR_CREATE: CalendarCreateArgs,
    TOOL_REMINDERS_READ: RemindersReadArgs,
    TOOL_REMINDERS_CREATE: RemindersCreateArgs,
    TOOL_SITE_WRITE_FILE: SiteWriteFileArgs,
    TOOL_SITE_PREVIEW: SitePreviewArgs,
    TOOL_SITE_LIST: SiteListArgs,
    TOOL_SITE_READ: SiteReadArgs,
    TOOL_SITE_DELETE: SiteDeleteArgs,
    TOOL_TIME_NOW: TimeNowArgs,
    TOOL_QUIZ_GENERATE: QuizGenerateArgs,
    TOOL_IMAGE_GENERATE: ImageGenerateArgs,
}


# Human-readable tool descriptions — single source of truth for both the Anthropic tool
# definitions and the GET /v1/tools catalog (ADR-019).
TOOL_DESCRIPTIONS: dict[str, str] = {
    TOOL_FILES_READ: "Read a file from the user's device.",
    TOOL_FILES_WRITE: "Write a file on the user's device.",
    TOOL_FILES_LIST: "List files/directories on the user's device.",
    TOOL_FILES_MKDIR: "Create a directory on the user's device.",
    TOOL_CALENDAR_READ: (
        "Read calendar events within a time range. 'start' and 'end' are ISO8601 datetime "
        "strings in local time without timezone offset, e.g. '2026-06-11T09:00:00'. For a "
        "whole day use start at 00:00:00 and end at the next day 00:00:00 (end-exclusive). "
        "Use the time.now tool if you do not know the current date."
    ),
    TOOL_CALENDAR_CREATE: (
        "Create calendar events. Each event's 'start' and 'end' are ISO8601 datetime strings "
        "in local time without timezone offset, e.g. '2026-06-11T09:00:00'."
    ),
    TOOL_REMINDERS_READ: "Read reminders.",
    TOOL_REMINDERS_CREATE: "Create reminders.",
    TOOL_SITE_WRITE_FILE: (
        "Write or overwrite a file in the website project. Path is relative to the project "
        "root. Use encoding 'utf8' for text (HTML/CSS/JS) and 'base64' for binary assets "
        "(images/fonts). The project is the current chat session's project (no project id "
        "needed)."
    ),
    TOOL_SITE_PREVIEW: (
        "Get a temporary signed preview URL for the current website project. Optional 'entry' "
        "selects the start file (default index.html). The returned `url` is an ABSOLUTE URL that "
        "opens directly in a browser (signed token, no authentication). Use it exactly as "
        "returned — do NOT change, shorten, or add a host/domain to it."
    ),
    TOOL_SITE_LIST: "List the files of the current website project.",
    TOOL_SITE_READ: "Read a file from the current website project by relative path.",
    TOOL_SITE_DELETE: "Delete a file from the current website project by relative path.",
    TOOL_TIME_NOW: (
        "Get the current date and time. Always returns UTC (ISO8601, unix timestamp, weekday). "
        "Pass an optional IANA timezone 'tz' (e.g. 'Europe/Moscow') to also get the local time. "
        "Call this whenever the request depends on the current date, time, or day of the week — "
        "do not guess."
    ),
    TOOL_QUIZ_GENERATE: (
        "Generate ONE interactive quiz card to check the learner's understanding of what you just "
        "explained. Call this once at a natural checkpoint — after teaching a concept or finishing "
        "a section — not on every message and never more than one quiz per point. Provide a clear "
        "'question' (up to 1000 characters), between 2 and 10 answer 'options' (each up to 400 "
        "characters), the 0-based 'correctIndex' of the right option (must be a valid index into "
        "'options'), and a short 'explanation' (up to 2000 characters) of why that answer is "
        "correct. Keep writing your normal teaching text as well: the quiz complements the "
        "explanation, it does not replace it."
    ),
    TOOL_IMAGE_GENERATE: (
        "Generate an image from a text prompt and return a reference to it (the bytes are stored "
        "server-side; the user fetches them separately). Provide a clear, detailed 'prompt' "
        "(up to 4000 characters) describing the image. Optionally set 'size' (one of "
        "'1024x1024', '1024x1536', '1536x1024', 'auto') and 'quality' (one of 'low', 'medium', "
        "'high', 'auto'); omit them to use the instance defaults. Each generated image costs extra "
        "credits, so generate one only when the user actually asks for an image."
    ),
}


def validate_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate Claude-produced tool args against the strict schema. Raises ValueError."""
    model = _ARGS_BY_TOOL.get(tool_name)
    if model is None:
        raise ValueError(f"unknown tool: {tool_name}")
    return model.model_validate(args).model_dump()


def tool_input_schema(tool_name: str) -> dict[str, Any]:
    """JSON Schema of a tool's args (``model_json_schema()`` of its model), title stripped."""
    schema = _ARGS_BY_TOOL[tool_name].model_json_schema()
    schema.pop("title", None)
    return schema


def tool_catalog() -> list[dict[str, Any]]:
    """Machine-readable catalog of all backend tools for GET /v1/tools (ADR-019).

    Single source of truth: iterates ``_ARGS_BY_TOOL`` (deterministic order). Each entry carries
    the dotted domain ``name`` (NOT the anthropic-underscore transport name), description,
    ``mutating`` (name in MUTATING_TOOLS), ``execution`` ("server" for SERVER_SIDE_TOOLS ∪
    GLOBAL_SERVER_SIDE_TOOLS else "client", ADR-026 §2) and ``inputSchema`` (the args JSON Schema).
    """
    catalog: list[dict[str, Any]] = []
    for name in _ARGS_BY_TOOL:
        catalog.append(
            {
                "name": name,
                "description": TOOL_DESCRIPTIONS[name],
                "mutating": name in MUTATING_TOOLS,
                "execution": (
                    "server"
                    if name in SERVER_SIDE_TOOLS or name in GLOBAL_SERVER_SIDE_TOOLS
                    else "client"
                ),
                "inputSchema": tool_input_schema(name),
            }
        )
    return catalog


def _is_client_side(name: str) -> bool:
    """True when ``name`` is a client-side tool (executed on the iOS device, ADR-011/ADR-026).

    Client-side = a tool in neither ``SERVER_SIDE_TOOLS`` (project-scoped ``site.*``) nor
    ``GLOBAL_SERVER_SIDE_TOOLS`` (``time.now`` / ``quiz.generate``). The
    ``include_client_side=False`` gate (ADR-056: temporary chat) drops exactly these; server-side
    tools stay offered — they execute in-request without DB continuation.
    """
    return name not in SERVER_SIDE_TOOLS and name not in GLOBAL_SERVER_SIDE_TOOLS


def _dialog_mode_excludes(name: str, dialog_mode: str | None) -> bool:
    """True when ``name`` must be dropped from the offered set for this ``dialog_mode`` (ADR-057).

    Only ``quiz.generate`` is dialog-mode-gated: it is offered ONLY when ``dialog_mode`` is
    ``study_learn`` (ADR-055 §5 / ADR-057 §4) so the quiz never «протекает» into other modes. Every
    other tool ignores ``dialog_mode``. This gate is orthogonal to and composes (logical AND) with
    ``include_server_side`` (axis A) and ``include_client_side`` (ADR-056) — quiz.generate is a
    GLOBAL server-side tool, so those two flags never drop it; this gate is what confines it to
    study_learn.
    """
    return name == TOOL_QUIZ_GENERATE and dialog_mode != DIALOG_MODE_STUDY_LEARN


def _openai_key_missing_excludes(name: str) -> bool:
    """True when ``name`` needs an OpenAI key that is NOT configured (ADR-058 §3, key-gate).

    ONLY ``image.generate`` is key-gated. Its client (``OpenAIImageGenerator``) always calls OpenAI
    regardless of ``LLM_PROVIDER`` — it is a SEPARATE ``AsyncOpenAI`` (``OPENAI_API_KEY``), NOT part
    of the ADR-033 provider abstraction — so an instance with an EMPTY ``OPENAI_API_KEY`` cannot
    generate images. Offering the tool there would waste tokens and a whole tool-round the user pays
    for on a call guaranteed to fail. Gate by the KEY, not by ``LLM_PROVIDER`` (an Anthropic-chat
    instance WITH an OpenAI key CAN generate — ADR-058 §3, revised). «Configured» = non-empty after
    ``strip()`` (the field defaults to ``""``). This is the FOURTH offered-set gate; it composes by
    logical AND with ``include_server_side`` / ``include_client_side`` / ``dialog_mode`` (each gate
    independently drops a tool). ``image.generate`` is a GLOBAL server-side tool, so the first two
    never drop it and it is not dialog-mode-gated — this key-gate is what confines it to instances
    that can actually serve it.
    """
    if name != TOOL_IMAGE_GENERATE:
        return False
    return not get_settings().openai_api_key.strip()


def anthropic_tool_definitions(
    *,
    include_server_side: bool = True,
    include_client_side: bool = True,
    dialog_mode: str | None = None,
) -> list[dict[str, Any]]:
    """Tool definitions for the Anthropic messages API (input_schema per tool).

    ADR-022 (axis A — project presence): when ``include_server_side`` is False, PROJECT-SCOPED
    server-side ``site.*`` tools (``SERVER_SIDE_TOOLS``) are EXCLUDED from the offered set — Claude
    never sees them and cannot call them. The orchestrator passes ``include_server_side=False`` for
    «чистый чат» sessions (``chat_sessions.project_id IS NULL``) and ``True`` when a project is
    present.

    ADR-026 §3: the ``include_server_side`` flag gates ONLY project-scoped ``SERVER_SIDE_TOOLS``
    (``site.*``). GLOBAL server-side tools (``GLOBAL_SERVER_SIDE_TOOLS`` — ``time.now``) are NEVER
    excluded by this flag — they are offered to Claude ALWAYS, with or without a project, in both
    assistant_modes (utility tool, axis B does not filter it).

    Note (Q-012-1 — Open): the orthogonal assistant_mode filter (axis B) is NOT yet implemented in
    code. Until it is, the effective offer-set = this project_id gate over the current behavior
    (all client-side tools always offered; site.* gated only by project presence; time.now always
    offered). When axis B lands, it composes by logical AND with this flag (time.now stays exempt).
    """
    definitions: list[dict[str, Any]] = []
    for name in _ARGS_BY_TOOL:
        if not include_server_side and name in SERVER_SIDE_TOOLS:
            # Axis A gate: drop project-scoped site.* when the session has no project (ADR-022 §2).
            # GLOBAL_SERVER_SIDE_TOOLS (time.now) are deliberately NOT under this gate (ADR-026 §3).
            continue
        if not include_client_side and _is_client_side(name):
            # ADR-056: temporary chat drops client-side tools (they need DB continuation);
            # server-side (site.* / time.now) stay offered (executed in-request).
            continue
        if _dialog_mode_excludes(name, dialog_mode):
            # ADR-057 §4: quiz.generate is offered only in study_learn.
            continue
        if _openai_key_missing_excludes(name):
            # ADR-058 §3 key-gate: drop image.generate when OPENAI_API_KEY is unset.
            continue
        definitions.append(
            {
                # BUG-3 forward map: Anthropic requires underscore names; iOS-facing names stay
                # dotted. `name` here is the domain name; emit the anthropic-name transport-side.
                "name": to_anthropic_tool_name(name),
                "description": TOOL_DESCRIPTIONS[name],
                "input_schema": tool_input_schema(name),
            }
        )
    return definitions


def neutral_tool_definitions(
    *,
    include_server_side: bool = True,
    include_client_side: bool = True,
    dialog_mode: str | None = None,
) -> list[dict[str, Any]]:
    """Provider-neutral tool definitions (ADR-033 §4): ``{name(domain dotted), description,
    input_schema}``.

    Single source of truth handed to ``LLMClient.create_message``; the client serializes them to
    its provider wire format (Anthropic underscore names / OpenAI function-tool wrapper).
    The ``include_server_side`` gate is identical to ``anthropic_tool_definitions`` (ADR-022 axis A:
    drop project-scoped ``site.*`` when there is no project; ``GLOBAL_SERVER_SIDE_TOOLS`` like
    ``time.now`` are never gated — ADR-026 §3). The ``include_client_side`` gate (ADR-056) drops
    client-side tools (``files.*``/``calendar.*``/``reminders.*``) for a temporary chat; server-side
    tools are unaffected. The ``dialog_mode`` gate (ADR-057 §4) drops the GLOBAL server-side
    ``quiz.generate`` unless ``dialog_mode == "study_learn"`` — the orchestrator forwards the
    session-fixed dialog mode so the quiz tool is offered only in Study & Learn.
    """
    definitions: list[dict[str, Any]] = []
    for name in _ARGS_BY_TOOL:
        if not include_server_side and name in SERVER_SIDE_TOOLS:
            continue
        if not include_client_side and _is_client_side(name):
            continue
        if _dialog_mode_excludes(name, dialog_mode):
            # ADR-057 §4: quiz.generate is offered only in study_learn.
            continue
        if _openai_key_missing_excludes(name):
            # ADR-058 §3 key-gate: drop image.generate when OPENAI_API_KEY is unset (also covers
            # openai_tool_definitions, which serializes THIS neutral set).
            continue
        definitions.append(
            {
                # Domain (dotted) name — the client maps it to the provider transport name.
                "name": name,
                "description": TOOL_DESCRIPTIONS[name],
                "input_schema": tool_input_schema(name),
            }
        )
    return definitions


# ADR-057 §2: domain tools serialized to OpenAI with ``strict: True``. A strict function tool
# guarantees ``arguments`` match the JSON schema without forcing the whole response into JSON — but
# OpenAI's strict-mode validator accepts only a SUBSET of JSON Schema and rejects the request with
# ``400 Invalid schema`` if any unsupported keyword is present (see below). Kept as a set so a later
# strict tool (e.g. image.generate, ADR-058) is one entry away.
_OPENAI_STRICT_TOOLS = frozenset({TOOL_QUIZ_GENERATE})

# ALLOWLIST (deliberately NOT a denylist) of JSON Schema keywords kept when serializing a strict
# tool's schema to OpenAI. OpenAI strict-mode (structured outputs / strict function tools) accepts
# only a SUBSET of JSON Schema and rejects the request with ``400 Invalid schema`` (→ UpstreamError
# → 502 on EVERY turn) if ANY unsupported keyword is present. A denylist is structurally unsafe: it
# must enumerate every forbidden keyword, and Pydantic v2 routinely emits ones easy to miss —
# ``allOf`` (``$ref`` + sibling metadata), ``anyOf``/``$defs``/``$ref`` (``Optional[...]``),
# ``patternProperties``, ``if``/``then``/``else``, ``not``, ``dependentRequired``,
# ``contentEncoding``… A missed keyword sails through and 400s at runtime, invisible to fake-client
# unit tests. An allowlist inverts the risk: an UNKNOWN keyword is DROPPED (safe — at worst we lose
# a soft hint the server-side Pydantic model still enforces) rather than forwarded (unsafe — a
# hard 400). This is the structural guarantee that protects a future strict tool (e.g.
# image.generate with ``size: str | None`` → ``anyOf`` — ADR-058) by construction, not by
# discipline. ``required`` and ``additionalProperties`` are strict-REQUIRED and MUST stay;
# ``enum``/``const``/``anyOf``/``$ref``/``$defs`` are strict-SUPPORTED and must survive (a future
# ``Literal``/``Enum``/optional field must not be silently broken). Server-side validation
# constraints (``maxLength``/``minItems``/…) are intentionally NOT here — they live in the Pydantic
# model (``validate_tool_args``) and are echoed as words in the tool description.
_OPENAI_STRICT_ALLOWED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "items",
        "required",
        "additionalProperties",
        "description",
        "enum",
        "const",
        "anyOf",
        "$ref",
        "$defs",
    }
)


def _clean_for_openai_strict(node: Any) -> Any:
    """Recursively keep ONLY ``_OPENAI_STRICT_ALLOWED_KEYWORDS`` in a JSON Schema (allowlist).

    Pure function (returns a NEW structure; never mutates the input) so the same neutral def can be
    serialized for other providers without side effects, and so it is testable in isolation (no
    network). SCHEMA-AWARE: the allowlist filters keyword positions only — the values under
    ``properties``/``$defs`` are maps of ARBITRARY names → subschema (names are data, not keywords,
    and must NOT be filtered), and ``items``/``anyOf``/``additionalProperties`` carry nested
    subschemas that are cleaned recursively. Leaf keyword values (``type``/``required``/
    ``description``/``$ref`` and the DATA in ``enum``/``const``) are kept verbatim. The result stays
    a valid strict schema: ``type: object`` + full ``required`` + ``additionalProperties: false``
    are all preserved. Unsupported keywords (``title``, ``maxLength``, ``allOf``,
    ``patternProperties``…) are simply absent from the allowlist and thus dropped.
    """
    if not isinstance(node, dict):
        # Reached only via an explicit dispatch below with a non-schema value (defensive); keep it.
        return node
    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _OPENAI_STRICT_ALLOWED_KEYWORDS:
            continue
        if key in ("properties", "$defs"):
            # Map of arbitrary property/definition NAME → subschema. Keep the names; clean values.
            cleaned[key] = {
                name: _clean_for_openai_strict(subschema) for name, subschema in value.items()
            }
        elif key == "anyOf":
            # List of subschemas (e.g. Optional[...] → anyOf[schema, {"type": "null"}]).
            cleaned[key] = [_clean_for_openai_strict(subschema) for subschema in value]
        elif key == "items":
            # Subschema, or a list of subschemas (tuple/positional items).
            cleaned[key] = (
                [_clean_for_openai_strict(subschema) for subschema in value]
                if isinstance(value, list)
                else _clean_for_openai_strict(value)
            )
        elif key == "additionalProperties":
            # ``false`` (strict) or, in general, a subschema — clean only when it is a schema dict.
            cleaned[key] = _clean_for_openai_strict(value) if isinstance(value, dict) else value
        else:
            # type / required / description / $ref, and the literal DATA in enum / const: leaves —
            # never schema nodes, kept verbatim (do NOT recurse into enum/const values).
            cleaned[key] = value
    return cleaned


def openai_tool_function(neutral_def: dict[str, Any]) -> dict[str, Any]:
    """Serialize ONE neutral tool definition to the OpenAI Responses function-tool shape (ADR-059).

    Single source of truth for the OpenAI wire wrapping — used both by ``openai_tool_definitions``
    (the SSOT generator) and by ``OpenAIClient._serialize_tools`` on the live path, so the shape is
    defined in exactly one place.

    Responses ``FunctionToolParam`` is FLAT (ADR-059 §1): ``name``/``parameters``/``strict`` live at
    the TOP level — there is NO nested ``function`` wrapper (the key difference from the former Chat
    Completions shape). Confirmed by intro­spection of pinned SDK 1.109.1
    (``openai.types.responses.FunctionToolParam``: ``type``/``name``/``parameters``/``strict``/
    ``description``).

    Input: a neutral def ``{name(domain dotted), description, input_schema}``. A def already in the
    flat Responses shape (``type == "function"``) is passed through unchanged (idempotent for
    callers that pre-serialized). Output:
    ``{type:"function", name(underscore), description, parameters(=input_schema), strict}``.
    OpenAI function names match the SAME ``^[a-zA-Z0-9_-]{1,64}$`` constraint as Anthropic — dots
    are forbidden for both providers — so the underscore map (``to_anthropic_tool_name``) is reused;
    the name is provider-neutral by value (dot↔underscore).

    ``strict`` is per-tool (ADR-057 §2, ``_OPENAI_STRICT_TOOLS``): ``quiz.generate`` is serialized
    ``strict: True``; every other tool stays ``strict: False`` (their arg schemas carry optional
    fields like ``tz``/``calendarId`` which strict-mode forbids). For a strict tool the
    ``parameters`` schema is passed through ``_clean_for_openai_strict`` FIRST: strict-mode accepts
    only a subset of JSON Schema, so the schema is reduced to the allowlist
    ``_OPENAI_STRICT_ALLOWED_KEYWORDS`` (unknown keywords — ``minLength``/``maxItems``/``allOf``/… —
    dropped by construction, so the wire schema cannot 400). Those constraints remain the
    server-side Pydantic validation
    (``QuizGenerateArgs`` via ``validate_tool_args``) and are echoed as words in the tool
    description. NON-strict tools are serialized with their schema UNCHANGED (they may keep
    ``minLength`` etc. — no strict validator runs on them).
    """
    if neutral_def.get("type") == "function":  # already flat Responses-shaped — pass through
        return neutral_def
    name = str(neutral_def.get("name", ""))
    # Same underscore transport name as Anthropic (dots forbidden on both).
    fn_name = to_anthropic_tool_name(name) if "." in name else name
    is_strict = name in _OPENAI_STRICT_TOOLS
    parameters = neutral_def["input_schema"]
    if is_strict:
        # Allowlist-clean to the strict-supported keyword subset (by construction the wire schema
        # cannot 400); keeps required + additionalProperties:false intact.
        parameters = _clean_for_openai_strict(parameters)
    return {
        "type": "function",
        "name": fn_name,
        "description": neutral_def["description"],
        "parameters": parameters,
        "strict": is_strict,
    }


def openai_tool_definitions(
    *,
    include_server_side: bool = True,
    include_client_side: bool = True,
    dialog_mode: str | None = None,
) -> list[dict[str, Any]]:
    """Tool definitions for the OpenAI Responses API (ADR-059 §1, flat function-tool shape).

    SSOT for the OpenAI offered tool-set: builds neutral defs (``neutral_tool_definitions``) and
    serializes each via ``openai_tool_function`` (the one OpenAI-wire wrapper). The
    ``include_server_side`` gate is identical to ``anthropic_tool_definitions`` (ADR-022 §A;
    ``GLOBAL_SERVER_SIDE_TOOLS`` never gated — ADR-026 §3); ``include_client_side`` drops
    client-side tools for a temporary chat (ADR-056); ``dialog_mode`` gates ``quiz.generate`` to
    study_learn (ADR-057 §4).
    """
    return [
        openai_tool_function(d)
        for d in neutral_tool_definitions(
            include_server_side=include_server_side,
            include_client_side=include_client_side,
            dialog_mode=dialog_mode,
        )
    ]
