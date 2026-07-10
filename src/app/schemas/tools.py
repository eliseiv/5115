"""Tools-catalog schema for GET /v1/tools (chat-orchestrator/02, ADR-019)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.schemas.common import StrictModel


class ToolDescriptor(StrictModel):
    name: str = Field(description="Доменное имя инструмента с точкой (например `files.read`).")
    description: str = Field(description="Назначение инструмента.")
    mutating: bool = Field(description="Меняет ли инструмент данные (требует подтверждения в UI).")
    execution: Literal["client", "server"] = Field(
        description="Где исполняется: `client` (на устройстве iOS) или `server` (на бэкенде)."
    )
    inputSchema: dict[str, Any] = Field(description="JSON Schema аргументов инструмента.")


class ToolsResponse(StrictModel):
    tools: list[ToolDescriptor] = Field(description="Полный каталог поддерживаемых инструментов.")
