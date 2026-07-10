"""Policy route: GET /v1/policy/effective (policy-engine/02)."""

from __future__ import annotations

from fastapi import APIRouter

from app.deps import CurrentUser, DbSession
from app.policy.loader import effective
from app.schemas.policy import EffectivePolicyResponse

router = APIRouter(prefix="/v1/policy", tags=["Policy"])


@router.get(
    "/effective",
    response_model=EffectivePolicyResponse,
    summary="Получить эффективные права",
    description=(
        "Возвращает текущие права пользователя для UI: есть ли подписка, остаток trial, баланс "
        "кредитов, включён ли BYOK, можно ли генерировать в режимах `credits`/`byok`. Поле "
        "`reasons[]` перечисляет причины недоступности (те же значения, что и `blockReason` в "
        "Chat), чтобы UI и `/v1/chat/run` были консистентны."
    ),
)
async def policy_effective(current: CurrentUser, session: DbSession) -> EffectivePolicyResponse:
    result = await effective(session, current.user_id)
    return EffectivePolicyResponse(
        isSubscribed=result.is_subscribed,
        trialRemaining=result.trial_remaining,
        creditsBalance=result.credits_balance,
        byokEnabled=result.byok_enabled,
        canGenerateCreditsMode=result.can_generate_credits_mode,
        canGenerateByokMode=result.can_generate_byok_mode,
        reasons=[r.value for r in result.reasons],
    )
