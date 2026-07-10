"""Tools catalog route: GET /v1/tools (chat-orchestrator/02, ADR-019).

JWT-protected like all /v1/* (CurrentUser). Returns the full backend tool registry sourced from
``app.chat.tools`` (single source of truth). Read-only; per-user rate limit as other reads.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.chat.tools import tool_catalog
from app.deps import CurrentUser
from app.errors import RateLimitedError
from app.schemas.tools import ToolsResponse

router = APIRouter(prefix="/v1/tools", tags=["Tools"])


@router.get(
    "",
    response_model=ToolsResponse,
    summary="Каталог инструментов",
    description=(
        "Возвращает список всех поддерживаемых инструментов tool-loop: имя, описание, флаг "
        "`mutating`, место исполнения (`client`/`server`) и JSON Schema аргументов."
    ),
)
async def list_tools(request: Request, current: CurrentUser) -> ToolsResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    return ToolsResponse.model_validate({"tools": tool_catalog()})
