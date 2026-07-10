"""Models catalog route: GET /v1/models (chat-orchestrator/02, ADR-034).

JWT-protected like GET /v1/tools (CurrentUser) — the list is not secret but the /v1/* auth contour
is uniform. Returns the active provider's model allowlist from ``Settings.allowed_models()`` as a
list of ``{id, displayName, default}``, with the instance default (``Settings.default_model()``)
marked ``default=true`` and emitted FIRST. Read-only; per-user rate limit as other reads.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.config import get_settings
from app.deps import CurrentUser
from app.errors import RateLimitedError
from app.schemas.models import ModelInfo, ModelsResponse

router = APIRouter(prefix="/v1/models", tags=["Models"])


def _build_models() -> list[ModelInfo]:
    """Ordered model list: default first, then the allowlist in insertion order (ADR-034 §2).

    ``allowed_models()`` already applies the empty-allowlist fallback (single default entry). The
    default model is ALWAYS present in the result (it is the fallback value, or it is prepended here
    if a non-empty allowlist does not contain it), is marked ``default=true`` and is emitted first;
    every other model keeps the allowlist insertion order without a duplicate default.
    """
    settings = get_settings()
    allowed = settings.allowed_models()
    default_id = settings.default_model()
    # displayName of the default: from the allowlist if present, else the id itself.
    default_display = allowed.get(default_id, default_id)
    models: list[ModelInfo] = [ModelInfo(id=default_id, displayName=default_display, default=True)]
    for model_id, display_name in allowed.items():
        if model_id == default_id:
            continue
        models.append(ModelInfo(id=model_id, displayName=display_name, default=False))
    return models


@router.get(
    "",
    response_model=ModelsResponse,
    summary="Доступные модели инстанса",
    description=(
        "Возвращает модели активного провайдера этого инстанса для селектора модели. Ровно одна "
        "помечена `default:true` (дефолтная модель инстанса) и идёт первой. `id` передаётся "
        "обратно в `POST /v1/chat/run` поле `model`."
    ),
)
async def list_models(request: Request, current: CurrentUser) -> ModelsResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    return ModelsResponse(models=_build_models())
