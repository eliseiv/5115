"""Unit: locale resolution for GET /v1/presets (ADR-049 §3/§4).

Two pure concerns, tested without I/O:
- ``resolve_presets_locale(query, accept_language, default)`` — router helper, strict priority
  (query → Accept-Language → default → "en"); an explicit unsupported ``?locale=`` raises
  ``ValidationFailedError`` (→ 422), while an unsupported ``Accept-Language`` degrades silently.
- ``Settings.resolved_presets_default_locale()`` — config helper, graceful (a value outside the
  supported set degrades to "en" + WARNING, never a crash).

Settings is constructed directly with alias kwargs (same hermetic pattern as
test_model_selection_config_adr034.py) so the config cases are independent of the process env.
"""

from __future__ import annotations

import logging

import pytest

from app.api_gateway.routers.presets import (
    _first_supported_language,
    resolve_presets_locale,
)
from app.config import Settings
from app.errors import ValidationFailedError

_DEFAULT = "en"


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


# ============================ resolve_presets_locale — query wins ============================
def test_query_locale_ru_selected() -> None:
    assert resolve_presets_locale("ru", None, _DEFAULT) == "ru"


def test_query_locale_en_selected() -> None:
    assert resolve_presets_locale("en", None, _DEFAULT) == "en"


def test_query_locale_uppercase_normalized() -> None:
    assert resolve_presets_locale("RU", None, _DEFAULT) == "ru"


def test_query_locale_whitespace_normalized() -> None:
    assert resolve_presets_locale("  ru  ", None, _DEFAULT) == "ru"


def test_query_locale_beats_accept_language() -> None:
    # Query is a strict, client-controlled intent → outranks the header.
    assert resolve_presets_locale("en", "ru-RU", _DEFAULT) == "en"


def test_query_locale_unsupported_raises_422() -> None:
    with pytest.raises(ValidationFailedError) as exc:
        resolve_presets_locale("de", "ru-RU", _DEFAULT)
    assert exc.value.status_code == 422
    assert exc.value.code == "validation_error"
    assert "de" in str(exc.value)


def test_query_locale_empty_string_unsupported_raises_422() -> None:
    # An empty ?locale= is still "present" (not None) → strict validation → 422.
    with pytest.raises(ValidationFailedError):
        resolve_presets_locale("", None, _DEFAULT)


# ============================ resolve_presets_locale — Accept-Language ============================
def test_accept_language_ru_region_maps_to_ru() -> None:
    assert resolve_presets_locale(None, "ru-RU", _DEFAULT) == "ru"


def test_accept_language_en_region_maps_to_en() -> None:
    assert resolve_presets_locale(None, "en-US", _DEFAULT) == "en"


def test_accept_language_order_and_qweight_first_supported_wins() -> None:
    # fr is unsupported → skipped; ru-RU (with q-weight) is the first supported primary-subtag.
    assert resolve_presets_locale(None, "fr-FR,ru-RU;q=0.8", "en") == "ru"


def test_accept_language_no_supported_falls_through_to_default() -> None:
    # Only unsupported tags → silent fall-through to the per-instance default (ru here).
    assert resolve_presets_locale(None, "fr-FR,de-DE", "ru") == "ru"


def test_accept_language_unparseable_falls_through_to_default() -> None:
    assert resolve_presets_locale(None, ";;;garbage;;;", "ru") == "ru"


def test_accept_language_empty_falls_through_to_default() -> None:
    assert resolve_presets_locale(None, "", "ru") == "ru"


# ============================ resolve_presets_locale — default & final fallback ============
def test_default_used_when_no_query_no_header() -> None:
    assert resolve_presets_locale(None, None, "ru") == "ru"


def test_default_en_when_nothing_provided() -> None:
    assert resolve_presets_locale(None, None, "en") == "en"


def test_unsupported_default_falls_back_to_en() -> None:
    # A caller passing a bogus default (defensive) still yields the "en" canon.
    assert resolve_presets_locale(None, None, "zz") == "en"


def test_priority_query_over_header_over_default() -> None:
    # All three present: query wins.
    assert resolve_presets_locale("ru", "en-US", "en") == "ru"
    # No query: header wins over default.
    assert resolve_presets_locale(None, "ru-RU", "en") == "ru"
    # No query, unsupported header: default wins.
    assert resolve_presets_locale(None, "fr-FR", "ru") == "ru"


# ============================ _first_supported_language ============================
def test_first_supported_language_none_for_missing_header() -> None:
    assert _first_supported_language(None) is None


def test_first_supported_language_none_for_no_supported() -> None:
    assert _first_supported_language("fr-FR,de-DE") is None


def test_first_supported_language_picks_first_supported() -> None:
    assert _first_supported_language("de,ru-RU,en") == "ru"


def test_first_supported_language_strips_qweight() -> None:
    assert _first_supported_language("ru-RU;q=0.9") == "ru"


# ============================ Settings.resolved_presets_default_locale ============================
def test_config_default_locale_ru() -> None:
    s = _settings(PRESETS_DEFAULT_LOCALE="ru")
    assert s.resolved_presets_default_locale() == "ru"


def test_config_default_locale_unset_is_en() -> None:
    s = _settings()
    assert s.resolved_presets_default_locale() == "en"


def test_config_default_locale_normalized() -> None:
    s = _settings(PRESETS_DEFAULT_LOCALE="  RU ")
    assert s.resolved_presets_default_locale() == "ru"


def test_config_default_locale_out_of_set_degrades_to_en_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    s = _settings(PRESETS_DEFAULT_LOCALE="xx")
    with caplog.at_level(logging.WARNING, logger="app.config"):
        resolved = s.resolved_presets_default_locale()
    assert resolved == "en"  # graceful fallback, no crash
    assert any(
        record.levelno == logging.WARNING and "xx" in record.getMessage()
        for record in caplog.records
    ), f"expected a WARNING mentioning the bad value, got {caplog.records!r}"


def test_config_default_locale_out_of_set_does_not_raise() -> None:
    # Mis-configured env must never crash the process (ADR-049 §4).
    s = _settings(PRESETS_DEFAULT_LOCALE="klingon")
    assert s.resolved_presets_default_locale() == "en"
