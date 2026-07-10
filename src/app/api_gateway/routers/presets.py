"""Presets catalog route: GET /v1/presets (chat-orchestrator/02, ADR-035, localized ADR-049).

JWT-protected like GET /v1/tools and GET /v1/models (CurrentUser) — the list is not secret but
the /v1/* auth contour is uniform. Returns the prompt-preset registry sourced from
``app.chat.presets`` (single source of truth). Read-only, no state/DB/ledger; per-user rate limit
as other reads. Provider-agnostic — identical on every instance for a given locale (ADR-033).

Locale resolution (ADR-049 §3), first match wins: explicit ``?locale=`` (invalid → 422) →
``Accept-Language`` (lenient, silent fallback) → per-instance ``PRESETS_DEFAULT_LOCALE`` (graceful)
→ ``en`` (canon). Resolution is a pure helper (``resolve_presets_locale``) for testability.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, Query, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.chat.presets import (
    DEFAULT_PRESET_LOCALE,
    SUPPORTED_PRESET_LOCALES,
    preset_catalog,
)
from app.config import get_settings
from app.deps import CurrentUser
from app.errors import RateLimitedError, ValidationFailedError
from app.schemas.presets import PresetsResponse

router = APIRouter(prefix="/v1/presets", tags=["Presets"])


def resolve_presets_locale(
    query_locale: str | None,
    accept_language: str | None,
    default_locale: str,
) -> str:
    """Resolve the catalog locale by priority (ADR-049 §3). Pure — no I/O, no settings access.

    Order (first match wins):
      1. ``query_locale`` (explicit ``?locale=``) — normalized ``strip().lower()``; must be in
         ``SUPPORTED_PRESET_LOCALES``. Present-but-unsupported → ``ValidationFailedError`` (422):
         an explicit client intent must not be silently substituted (symmetric to unsupported_model,
         ADR-034 §3).
      2. ``accept_language`` — first supported primary-subtag (``ru-RU`` → ``ru``); ``q``-weights
         are dropped. No supported subtag / blank / unparseable → silent fallback (no error), the
         header is not a strict client intent.
      3. ``default_locale`` — the per-instance default (already graceful, ADR-049 §4), if supported.
      4. ``DEFAULT_PRESET_LOCALE`` (``"en"``) — final canon fallback.
    """
    if query_locale is not None:
        normalized = query_locale.strip().lower()
        if normalized in SUPPORTED_PRESET_LOCALES:
            return normalized
        raise ValidationFailedError(f"locale '{query_locale}' is not supported")

    header_locale = _first_supported_language(accept_language)
    if header_locale is not None:
        return header_locale

    if default_locale in SUPPORTED_PRESET_LOCALES:
        return default_locale
    return DEFAULT_PRESET_LOCALE


def _first_supported_language(accept_language: str | None) -> str | None:
    """First supported primary-subtag from an ``Accept-Language`` header, else ``None`` (lenient).

    Splits on ``,``, drops the ``;q=...`` weight, takes the part before ``-`` in lower case
    (``ru-RU`` → ``ru``), and returns the first tag present in ``SUPPORTED_PRESET_LOCALES``. A
    blank/unparseable header yields ``None`` (caller falls through). Standard content-negotiation
    leniency: never raises.
    """
    if not accept_language:
        return None
    for part in accept_language.split(","):
        tag = part.split(";", 1)[0].strip().lower()
        if not tag:
            continue
        primary = tag.split("-", 1)[0]
        if primary in SUPPORTED_PRESET_LOCALES:
            return primary
    return None


@router.get(
    "",
    response_model=PresetsResponse,
    summary="Каталог пресетов промтов",
    description=(
        "Возвращает список пресетов для чипов на главном экране чата: `id` (стабильный slug), "
        "`title`, `icon` (имя SF Symbol) и `prompt` (текст для подстановки в композер). Порядок "
        "элементов = порядок чипов на экране. Тексты `title` и `prompt` отдаются на выбранном "
        "языке: приоритет у параметра `locale`, затем заголовок `Accept-Language`, затем язык "
        "по умолчанию для инстанса; при отсутствии перевода используется английский. Поле `locale` "
        "в ответе сообщает фактически применённый язык. Read-only, без состояния."
    ),
)
async def list_presets(
    request: Request,
    current: CurrentUser,
    locale: str | None = Query(
        default=None,
        description=(
            "Желаемый язык каталога (например `en` или `ru`). Если не указан — язык определяется "
            "по заголовку `Accept-Language`, иначе используется язык по умолчанию для инстанса. "
            "Недопустимое значение возвращает ошибку 422."
        ),
        examples=["ru"],
    ),
    accept_language: str | None = Header(default=None),
) -> PresetsResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    resolved = resolve_presets_locale(
        query_locale=locale,
        accept_language=accept_language,
        default_locale=get_settings().resolved_presets_default_locale(),
    )
    return PresetsResponse.model_validate({"locale": resolved, "presets": preset_catalog(resolved)})
