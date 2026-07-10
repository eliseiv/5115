"""Prompt presets registry (ADR-035, localized by ADR-049): catalog for GET /v1/presets.

Single source of truth for the chat home-screen preset chips (Plan Week, Meeting Notes, …).
By the same pattern as ``tool_catalog()`` (``app.chat.tools``): a module-level static list +
a pure ``preset_catalog(locale)`` that returns the entries in declaration order (= chip order on
screen). No I/O, no state, no DB; provider/instance-agnostic — identical on every instance
(ADR-033). Editing presets without a deploy (config-JSON / DB) is deferred — TD-026.

Localization (ADR-049): ``id`` and ``icon`` are stable machine identifiers and are NOT localized
(client analytics/cache key by ``id``, ``icon`` is an SF Symbol resource); ``title`` and ``prompt``
carry one string per locale. EN is the canon and per-field fallback (Q-035-2 partially closed).

Each preset carries:
- ``id``    — stable snake_case slug (``[a-z0-9_]``), unique in the set; stable across releases.
- ``icon``  — SF Symbol name (ADR-035 §4); the iOS client renders it via ``Image(systemName:)``.
- ``title`` — ``locale -> chip display name``; key ``"en"`` is REQUIRED (canon/fallback).
- ``prompt``— ``locale -> composer text``; key ``"en"`` is REQUIRED (canon/fallback).
"""

from __future__ import annotations

from typing import Any, NamedTuple

# Supported preset locales — single source of truth (ADR-049 §1; EN first = canon/fallback).
# Extending = add the locale here AND fill title/prompt in the registry. Never hardcode "exactly 2".
SUPPORTED_PRESET_LOCALES: tuple[str, ...] = ("en", "ru")
# Canon and per-field fallback locale (ADR-049 §1). Its key is required in every preset.
DEFAULT_PRESET_LOCALE: str = "en"


class Preset(NamedTuple):
    """One prompt preset (ADR-035 §1, localized ADR-049 §1).

    ``id``/``icon`` are stable and locale-independent; ``title``/``prompt`` are ``locale -> str``
    maps whose ``"en"`` key is required (canon). All EN values are non-empty.
    """

    id: str
    icon: str
    title: dict[str, str]
    prompt: dict[str, str]


# Static registry — single source of truth (ADR-035 §2/§3, ADR-049 §1.1). Declaration order IS the
# chip order on the chat home screen. Editing without a deploy is intentionally out of scope
# (TD-026). EN strings are unchanged from ADR-035 §3; RU strings are the approved ADR-049 §1.1 set.
_PRESETS: tuple[Preset, ...] = (
    Preset(
        id="plan_week",
        icon="calendar",
        title={
            "en": "Plan Week",
            "ru": "Планирование недели",
        },
        prompt={
            "en": (
                "Help me plan my upcoming week. Ask me about my priorities, deadlines, and "
                "commitments, then propose a balanced day-by-day schedule."
            ),
            "ru": (
                "Помоги спланировать предстоящую неделю. Расспроси меня о приоритетах, сроках и "
                "обязательствах, а затем предложи сбалансированное расписание по дням."
            ),
        },
    ),
    Preset(
        id="meeting_notes",
        icon="person.2",
        title={
            "en": "Meeting Notes",
            "ru": "Заметки со встречи",
        },
        prompt={
            "en": (
                "Turn my raw meeting notes into a clean summary with key decisions, action items "
                "(with owners), and open questions. I'll paste the notes next."
            ),
            "ru": (
                "Преврати мои черновые заметки со встречи в аккуратное резюме: ключевые решения, "
                "задачи с ответственными и открытые вопросы. Я вставлю заметки следующим "
                "сообщением."
            ),
        },
    ),
    Preset(
        id="tasks_from_photo",
        icon="camera",
        title={
            "en": "Tasks from Photo",
            "ru": "Задачи с фото",
        },
        prompt={
            "en": (
                "I'll attach a photo of a note, whiteboard, or list. Extract every actionable task "
                "from it and return them as a clear checklist."
            ),
            "ru": (
                "Я прикреплю фото заметки, доски или списка. Выдели из него все конкретные "
                "задачи и верни их в виде понятного чек-листа."
            ),
        },
    ),
    Preset(
        id="design_brief",
        icon="paintbrush",
        title={
            "en": "Design Brief",
            "ru": "Дизайн-бриф",
        },
        prompt={
            "en": (
                "Help me write a concise design brief. Ask me about the goal, audience, scope, "
                "constraints, and success criteria, then draft the brief."
            ),
            "ru": (
                "Помоги составить лаконичный дизайн-бриф. Расспроси меня о цели, аудитории, объёме "
                "работ, ограничениях и критериях успеха, а затем подготовь бриф."
            ),
        },
    ),
    Preset(
        id="daily_review",
        icon="checklist",
        title={
            "en": "Daily Review",
            "ru": "Итоги дня",
        },
        prompt={
            "en": (
                "Guide me through a short daily review: what I accomplished, what's still open, "
                "and the top 3 priorities for tomorrow."
            ),
            "ru": (
                "Проведи меня через короткий разбор дня: что удалось сделать, что осталось "
                "незавершённым и какие три главных приоритета на завтра."
            ),
        },
    ),
    Preset(
        id="summarize_text",
        icon="doc.text",
        title={
            "en": "Summarize Text",
            "ru": "Краткое изложение",
        },
        prompt={
            "en": (
                "Summarize the text I provide. Give a 3-sentence overview, then key points as "
                "bullets. I'll paste the text next."
            ),
            "ru": (
                "Кратко изложи текст, который я пришлю. Дай обзор в трёх предложениях, а затем "
                "ключевые мысли списком. Я вставлю текст следующим сообщением."
            ),
        },
    ),
    Preset(
        id="project_structure",
        icon="folder",
        title={
            "en": "Project Structure",
            "ru": "Структура проекта",
        },
        prompt={
            "en": (
                "Help me design a project structure. Ask about the project type and goals, then "
                "propose a folder/file layout with a short rationale."
            ),
            "ru": (
                "Помоги продумать структуру проекта. Расспроси о типе проекта и целях, а затем "
                "предложи структуру папок и файлов с кратким обоснованием."
            ),
        },
    ),
)


def preset_catalog(locale: str) -> list[dict[str, Any]]:
    """Machine-readable catalog of prompt presets for the given locale (ADR-035, ADR-049 §2).

    Pure (no I/O): iterates the static ``_PRESETS`` registry in declaration order (= chip order)
    and returns a list of ``{id, title, icon, prompt}`` dicts. ``title``/``prompt`` are resolved
    for ``locale`` with a per-field EN fallback — an unknown locale or a field missing for the
    locale degrades to ``DEFAULT_PRESET_LOCALE`` (never an empty string). ``id``/``icon`` are
    locale-independent. Locale resolution itself is the router's concern (ADR-049 §3), not here.
    """
    return [
        {
            "id": p.id,
            "title": p.title.get(locale) or p.title[DEFAULT_PRESET_LOCALE],
            "icon": p.icon,
            "prompt": p.prompt.get(locale) or p.prompt[DEFAULT_PRESET_LOCALE],
        }
        for p in _PRESETS
    ]
