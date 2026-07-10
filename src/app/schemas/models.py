"""Models-catalog schema for GET /v1/models (chat-orchestrator/02, ADR-034).

Provider-agnostic response contract: the active provider's allowlist as a list of
``{id, displayName, default}`` items. Exactly one item has ``default=true`` (the instance default
model), which is emitted first. An empty allowlist yields a single default item (displayName = id) —
backward compatibility (ADR-034 §1–2).
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import StrictModel


class ModelInfo(StrictModel):
    id: str = Field(
        description=(
            "Провайдерный id модели. Передаётся обратно в `POST /v1/chat/run` поле `model`."
        )
    )
    displayName: str = Field(
        description="Человекочитаемое имя модели для UI (из allowlist `id→displayName`)."
    )
    default: bool = Field(
        description=(
            "Дефолтная модель инстанса. Ровно у одного элемента `true`; этот элемент идёт первым."
        )
    )


class ModelsResponse(StrictModel):
    models: list[ModelInfo] = Field(
        description="Доступные модели активного провайдера инстанса (дефолт первым)."
    )
