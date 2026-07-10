"""Unit tests for the prompt-presets registry (ADR-035, localized ADR-049).

``app.chat.presets.preset_catalog(locale)`` is pure (no I/O, no state, no DB) and
provider/instance-agnostic. It returns the seven static presets in declaration order (= chip
order on the chat home screen) as a list of ``{id, title, icon, prompt}`` dicts. ``id``/``icon``
are stable machine identifiers (NOT localized); ``title``/``prompt`` carry the resolved-locale
string with a per-field EN fallback.
"""

from __future__ import annotations

import re

from app.chat.presets import (
    DEFAULT_PRESET_LOCALE,
    SUPPORTED_PRESET_LOCALES,
    preset_catalog,
)

# Canonical order and id set (ADR-035 §2) — declaration order IS the chip order.
_EXPECTED_IDS = [
    "plan_week",
    "meeting_notes",
    "tasks_from_photo",
    "design_brief",
    "daily_review",
    "summarize_text",
    "project_structure",
]
# Stable SF-Symbol icons per id (ADR-035 §4, ADR-049 §1.1) — locale-independent.
_EXPECTED_ICONS = {
    "plan_week": "calendar",
    "meeting_notes": "person.2",
    "tasks_from_photo": "camera",
    "design_brief": "paintbrush",
    "daily_review": "checklist",
    "summarize_text": "doc.text",
    "project_structure": "folder",
}
# A few RU ground-truth strings (ADR-049 §1.1) asserted verbatim.
_RU_TITLES = {
    "plan_week": "Планирование недели",
    "meeting_notes": "Заметки со встречи",
    "summarize_text": "Краткое изложение",
    "project_structure": "Структура проекта",
}
_SNAKE_CASE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_FIELDS = ("id", "title", "icon", "prompt")


# ----------------------------- shape / order (per locale) -----------------------------
def test_preset_catalog_en_has_seven_entries() -> None:
    assert len(preset_catalog("en")) == 7


def test_preset_catalog_ru_has_seven_entries() -> None:
    assert len(preset_catalog("ru")) == 7


def test_preset_catalog_deterministic_order_en() -> None:
    ids = [p["id"] for p in preset_catalog("en")]
    assert ids == _EXPECTED_IDS
    # Calling twice yields the identical structure (no hidden state / shuffling).
    assert preset_catalog("en") == preset_catalog("en")


def test_preset_catalog_order_identical_across_locales() -> None:
    # Chip order (declaration order) is stable in every locale (ADR-049 §1 invariant).
    assert [p["id"] for p in preset_catalog("en")] == _EXPECTED_IDS
    assert [p["id"] for p in preset_catalog("ru")] == _EXPECTED_IDS


def test_preset_catalog_is_pure_no_shared_mutable_state() -> None:
    # Mutating a returned copy must not leak into the next call's result.
    first = preset_catalog("en")
    first[0]["title"] = "MUTATED"
    first.append({"id": "x", "title": "x", "icon": "x", "prompt": "x"})
    second = preset_catalog("en")
    assert len(second) == 7
    assert second[0]["title"] != "MUTATED"


def test_preset_catalog_all_four_fields_present_and_non_empty_every_locale() -> None:
    for locale in SUPPORTED_PRESET_LOCALES:
        for p in preset_catalog(locale):
            assert set(p.keys()) == set(_FIELDS), f"unexpected fields ({locale}) {p}"
            for field in _FIELDS:
                value = p[field]
                assert isinstance(value, str), f"{field} not a str ({locale}) on {p['id']}"
                assert value.strip(), f"{field} empty ({locale}) on preset {p['id']}"


def test_preset_catalog_ids_unique_snake_case() -> None:
    ids = [p["id"] for p in preset_catalog("en")]
    assert len(ids) == len(set(ids)), f"duplicate preset ids: {ids}"
    for pid in ids:
        assert _SNAKE_CASE.match(pid), f"id is not snake_case [a-z0-9_]: {pid!r}"


# ----------------------------- id/icon stable, title/prompt localized -----------------------------
def test_id_and_icon_identical_between_en_and_ru() -> None:
    en = preset_catalog("en")
    ru = preset_catalog("ru")
    assert [p["id"] for p in en] == [p["id"] for p in ru]
    assert [p["icon"] for p in en] == [p["icon"] for p in ru]
    # And the icons match the ADR-049 §1.1 ground truth.
    assert {p["id"]: p["icon"] for p in ru} == _EXPECTED_ICONS


def test_title_and_prompt_differ_between_en_and_ru() -> None:
    en = {p["id"]: p for p in preset_catalog("en")}
    ru = {p["id"]: p for p in preset_catalog("ru")}
    for pid in _EXPECTED_IDS:
        assert en[pid]["title"] != ru[pid]["title"], f"title not localized for {pid}"
        assert en[pid]["prompt"] != ru[pid]["prompt"], f"prompt not localized for {pid}"


def test_ru_titles_verbatim_from_adr() -> None:
    ru = {p["id"]: p["title"] for p in preset_catalog("ru")}
    for pid, expected in _RU_TITLES.items():
        assert ru[pid] == expected, f"RU title mismatch for {pid}: {ru[pid]!r}"


def test_ru_prompts_verbatim_key_examples() -> None:
    ru = {p["id"]: p["prompt"] for p in preset_catalog("ru")}
    assert ru["plan_week"] == (
        "Помоги спланировать предстоящую неделю. Расспроси меня о приоритетах, сроках и "
        "обязательствах, а затем предложи сбалансированное расписание по дням."
    )
    assert ru["summarize_text"] == (
        "Кратко изложи текст, который я пришлю. Дай обзор в трёх предложениях, а затем "
        "ключевые мысли списком. Я вставлю текст следующим сообщением."
    )


# ----------------------------- unknown locale → full EN fallback -----------------------------
def test_unknown_locale_falls_back_to_full_english() -> None:
    # An unknown locale ('zz') degrades every field to EN → byte-for-byte the EN catalog.
    assert preset_catalog("zz") == preset_catalog(DEFAULT_PRESET_LOCALE)
    assert preset_catalog("zz") == preset_catalog("en")


def test_default_locale_constant_is_en_and_supported() -> None:
    assert DEFAULT_PRESET_LOCALE == "en"
    assert DEFAULT_PRESET_LOCALE in SUPPORTED_PRESET_LOCALES
    assert SUPPORTED_PRESET_LOCALES[0] == "en"  # EN first = canon/fallback (ADR-049 §1)


def test_partial_locale_field_falls_back_to_en_per_field() -> None:
    # Per-field fallback: if a locale fills only part of the fields, the missing field degrades
    # to EN (never an empty string). Exercised via a synthetic registry entry so the guarantee
    # holds regardless of the shipped catalog being fully bilingual.
    from typing import Any

    from app.chat import presets as presets_mod

    original = presets_mod._PRESETS
    synthetic = presets_mod.Preset(
        id="synthetic",
        icon="star",
        # 'ru' has a title but NO prompt → prompt must fall back to EN.
        title={"en": "EN Title", "ru": "RU Заголовок"},
        prompt={"en": "EN prompt only"},
    )
    presets_mod._PRESETS = (synthetic,)
    try:
        ru: list[dict[str, Any]] = preset_catalog("ru")
    finally:
        presets_mod._PRESETS = original
    assert ru[0]["title"] == "RU Заголовок"  # locale value used where present
    assert ru[0]["prompt"] == "EN prompt only"  # missing field → EN fallback
