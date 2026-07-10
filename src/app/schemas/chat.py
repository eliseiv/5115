"""Chat schemas for /v1/chat/run and /v1/chat/tool-result (chat-orchestrator/02)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import Field, model_validator

from app.config import get_settings
from app.schemas.common import StrictModel

# Allowed mediaType values per attachment class (ADR-020, 05-security.md; Q-020-1 extension).
# Fixed in code as a server-side allowlist (not a denylist) — anything else => 422.
AttachmentMediaType = Literal[
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
]


class AttachmentIn(StrictModel):
    """Вложение в base64. Только inline base64 — ссылки (URL) не принимаются."""

    type: Literal["image", "document", "text"] = Field(
        description="Класс вложения: `image` (фото), `document` (PDF) или `text` (текстовый файл)."
    )
    mediaType: AttachmentMediaType = Field(
        description=(
            "MIME-тип содержимого из allowlist: `image/jpeg|png|gif|webp`, `application/pdf`, "
            "`text/plain|markdown|csv`, `application/json`. Вне списка → 422."
        )
    )
    filename: str | None = Field(
        default=None,
        max_length=512,
        description="Имя файла для человекочитаемой разметки (особенно для `text`-вложений).",
    )
    data: str = Field(
        min_length=1,
        description="Содержимое файла в base64. Только inline base64 — URL запрещены.",
    )


class TemporaryTurn(StrictModel):
    """Одна реплика клиентского транскрипта временного чата (ADR-056 §1).

    Провайдер-агностичный текст: клиент присылает только роль и текст, НЕ wire-формат провайдера
    (content-блоки Anthropic vs items OpenAI). Оркестратор сам собирает из транскрипта сообщения
    для активного клиента. Только `user`/`assistant` — сырые `tool_use`/`tool_result` не
    принимаются (непарный `tool_use` → 400 от провайдера, BUG-5 / ADR-021).
    """

    role: Literal["user", "assistant"] = Field(description="Роль реплики: `user` или `assistant`.")
    content: str = Field(
        min_length=1,
        description="Текст реплики (непустой). Только текст — без tool-блоков.",
    )


class ChatRunRequest(StrictModel):
    userId: uuid.UUID = Field(
        description="Идентификатор пользователя. Обязан совпадать с `sub` JWT."
    )
    projectId: str | None = Field(
        default=None,
        description=(
            "Идентификатор проекта. Опционален: без него создаётся чат без проекта. Если "
            "указан — должен быть непустой строкой. Фиксируется при создании сессии; при "
            "продолжении берётся из сессии, поле запроса игнорируется."
        ),
    )
    sessionId: uuid.UUID | None = Field(
        default=None,
        description="Идентификатор сессии диалога. Если не задан — создаётся новая сессия.",
    )
    workspaceProjectId: uuid.UUID | None = Field(
        default=None,
        description=(
            "Привязка чата к рабочему пространству (workspace). Опционально: без поля — "
            "чат без workspace (обратная совместимость). Если указано — при создании сессии "
            "валидируется принадлежность workspace пользователю (чужой/несуществующий → 404 "
            "workspace_not_found); `instructions` и файлы-знания подаются как контекст первого "
            "хода. Фиксируется при создании сессии; при продолжении берётся из сессии, поле "
            "запроса игнорируется. Не путать с `projectId` (website-builder, TEXT)."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "Выбор модели из allowlist активного провайдера (`GET /v1/models`). Опционально: без "
            "поля — дефолтная модель инстанса (обратная совместимость). Если указано — непустая "
            "строка после `strip` (пустая/whitespace → 422) и должна входить в allowlist (иначе "
            "422 unsupported_model). Фиксируется при создании сессии; при продолжении берётся из "
            "сессии, поле запроса игнорируется."
        ),
    )
    message: str = Field(
        default="",
        description="Текст сообщения пользователя. Опционален при наличии хотя бы одного вложения.",
    )
    mode: Literal["credits", "byok"] = Field(
        description=(
            "Режим тарификации: `credits` (кредиты подписки) или `byok` (свой ключ Anthropic). "
            "Не путать с `assistantMode` (тип ассистента)."
        ),
    )
    assistantMode: Literal["chat", "code"] | None = Field(
        default=None,
        description=(
            "Тип ассистента: `chat` или `code`. Опционально; при отсутствии берётся дефолт из "
            "настроек пользователя, затем `chat`. Фиксируется при создании сессии. "
            "Ортогонален `mode` (оплата)."
        ),
    )
    dialogMode: str | None = Field(
        default=None,
        description=(
            "Режим диалога (ADR-055): `smart` | `deep_thinking` | `study_learn` | `search`. "
            "Опционально; при отсутствии берётся дефолт из настроек пользователя, затем `smart`. "
            "Фиксируется при создании сессии; при продолжении берётся из сессии, поле запроса "
            "игнорируется. `deep_thinking`/`study_learn`/`search` требуют OpenAI-инстанса (иначе "
            "422 unsupported_dialog_mode). Тип `str` (не `Literal`) — членство проверяет "
            "оркестратор, чтобы вернуть машинный код, а не generic 422. Ортогонален `mode` "
            "(оплата) и `assistantMode` (тип ассистента)."
        ),
    )
    attachments: list[AttachmentIn] | None = Field(
        default=None,
        description=(
            "Вложения в base64 (фото/PDF/текст), отправляемые модели только в первом "
            "сообщении. Опционально. Только base64, URL запрещены. В `/v1/chat/tool-result` не "
            "принимаются."
        ),
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description="Опциональный контекст клиента (например, локаль). Ограничен по размеру.",
    )
    editMessageStepId: uuid.UUID | None = Field(
        default=None,
        description=(
            "ID хода для редактирования: история сессии усекается от него и генерируется новый "
            "ход. Требует sessionId."
        ),
    )
    temporary: bool = Field(
        default=False,
        description=(
            "Временный чат (ADR-056): ход НЕ персистится (ни `chat_sessions`/`chat_steps`/"
            "`tool_calls`), историю присылает клиент полем `history`. Client-side инструменты "
            "отключены (нужна континьюация через БД); server-side разрешены. Несовместим с "
            "`sessionId`/`editMessageStepId`/`projectId`/`workspaceProjectId` (→ 422)."
        ),
    )
    history: list[TemporaryTurn] | None = Field(
        default=None,
        description=(
            "Клиентский текстовый транскрипт для временного чата (ADR-056): список "
            "`{role, content}` (только `user`/`assistant`). Применим ТОЛЬКО при `temporary=true` "
            "(иначе 422). Пустой список допустим (первый ход). Суммарный размер ≤ "
            "`SIZE_LIMIT_CONTEXT`."
        ),
    )

    @model_validator(mode="after")
    def _check_sizes(self) -> ChatRunRequest:
        # ADR-056 §3: temporary chat is incompatible with anything that implies persistence/resume.
        # All → 422 (invalid request, not a business block).
        if self.temporary:
            if self.sessionId is not None:
                raise ValueError("temporary chat cannot be resumed (sessionId not allowed)")
            if self.editMessageStepId is not None:
                raise ValueError("temporary chat cannot edit a message (editMessageStepId)")
            if self.projectId is not None:
                raise ValueError("temporary chat cannot use a projectId")
            if self.workspaceProjectId is not None:
                raise ValueError("temporary chat cannot use a workspaceProjectId")
        # ADR-056 §3: the history transcript is only meaningful for a temporary chat (a normal chat
        # reads its history from the DB). history present without temporary → 422.
        if self.history is not None and not self.temporary:
            raise ValueError("history is only allowed for a temporary chat (temporary=true)")
        # ADR-056 §3: the transcript's FIRST turn must be `user`. Both providers require the first
        # message of a conversation to be from the user (Anthropic rejects an assistant-first
        # history with 400 → the client would get an opaque 502 instead of an honest 422); temporary
        # chat is NOT provider-gated (temporary ∉ ADR-055 gated modes), so the Anthropic path is
        # real. Only the first turn is checked — NOT strict alternation: both providers accept
        # consecutive same-role turns (Anthropic merges them), so requiring alternation would reject
        # legitimate transcripts. A history ENDING on `user` is normal — the current `message`
        # follows it.
        if self.history and self.history[0].role != "user":
            raise ValueError("history must start with a user turn")
        # ADR-056 §3: bound the client-supplied transcript by the same context size limit.
        if self.history is not None:
            settings = get_settings()
            history_bytes = sum(len(turn.content.encode("utf-8")) for turn in self.history)
            if history_bytes > settings.size_limit_context:
                raise ValueError("history exceeds size limit")
        # ADR-040 §1: editMessageStepId is only valid within an existing (resume) session — it
        # cannot be combined with creating a new session. Missing sessionId → 422.
        if self.editMessageStepId is not None and self.sessionId is None:
            raise ValueError("editMessageStepId requires sessionId")
        # ADR-022: projectId is optional (default None) → «чистый чат». When present it must be a
        # non-empty string (a blank projectId is rejected rather than silently treated as NULL).
        if self.projectId is not None and not self.projectId.strip():
            raise ValueError("projectId must be a non-empty string when provided")
        # ADR-034 §3: model is optional; when present it must be a non-empty string after strip
        # (a blank model is rejected → 422, symmetric to projectId). Allowlist membership is
        # validated in the orchestrator at session creation (needs settings.allowed_models()).
        if self.model is not None and not self.model.strip():
            raise ValueError("model must be a non-empty string when provided")
        # ADR-055 §6: dialogMode is optional; when present it must be a non-empty string after
        # strip (a blank one → 422, symmetric to model). Membership in the enum + the provider-gate
        # are validated in the orchestrator at session creation → 422 unsupported_dialog_mode (a
        # machine code, not a generic 422 — that is why the field is `str`, not `Literal`).
        if self.dialogMode is not None and not self.dialogMode.strip():
            raise ValueError("dialogMode must be a non-empty string when provided")
        # ADR-039 §1: message is optional but the turn must carry content. Valid iff message is
        # non-empty after strip OR at least one attachment is present in the request. A
        # whitespace-only message with no attachment is also rejected (→ 422). The size limit below
        # still applies (an empty string trivially passes); a non-empty message is unchanged.
        if not self.message.strip() and not self.attachments:
            raise ValueError("message or at least one attachment is required")
        settings = get_settings()
        if len(self.message.encode("utf-8")) > settings.size_limit_message:
            raise ValueError("message exceeds size limit")
        if self.context is not None:
            import json

            if len(json.dumps(self.context).encode("utf-8")) > settings.size_limit_context:
                raise ValueError("context exceeds size limit")
        return self


class ToolErrorBody(StrictModel):
    code: str = Field(description="Машиночитаемый код ошибки исполнения инструмента.")
    message: str = Field(description="Человекочитаемое описание ошибки инструмента.")


class ToolResultItem(StrictModel):
    """Один элемент батча tool-результатов."""

    toolCallId: uuid.UUID = Field(
        description="Идентификатор вызова инструмента — равен `toolCalls[].id` из `/v1/chat/run`."
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="Результат исполнения инструмента. В элементе ровно одно из `result`/`error`.",
    )
    error: ToolErrorBody | None = Field(
        default=None,
        description="Ошибка исполнения инструмента. В элементе ровно одно из `result`/`error`.",
    )

    @model_validator(mode="after")
    def _check_one_of(self) -> ToolResultItem:
        if (self.result is None) == (self.error is None):
            raise ValueError("exactly one of result/error is required per item")
        if self.result is not None:
            import json

            settings = get_settings()
            if len(json.dumps(self.result).encode("utf-8")) > settings.size_limit_tool_result:
                raise ValueError("result exceeds size limit")
        return self


class ChatToolResultRequest(StrictModel):
    """Приём результата(ов) tools.

    Батч-форма (`results[]`) — рекомендуемая: результаты на все `toolCalls[]` одного хода.
    Одиночная форма (`toolCallId` + `result|error` на верхнем уровне) — **deprecated**,
    обратная совместимость; нормализуется в батч из одного элемента (`normalized_results`).
    Указывается ровно одна из двух форм.
    """

    userId: uuid.UUID = Field(
        description="Идентификатор пользователя. Обязан совпадать с `sub` JWT."
    )
    sessionId: uuid.UUID = Field(
        description="Идентификатор сессии, в рамках которой шёл tool-loop."
    )
    # Батч-форма (рекомендуемая, ADR-025).
    results: list[ToolResultItem] | None = Field(
        default=None,
        description=(
            "Результаты на один или несколько tool-вызовов одного хода. Рекомендуемая форма для "
            "parallel tool use. В каждом элементе ровно одно из `result`/`error`."
        ),
    )
    # Одиночная форма (deprecated, обратная совместимость).
    toolCallId: uuid.UUID | None = Field(
        default=None,
        description=(
            "DEPRECATED (используйте `results[]`). Идентификатор вызова инструмента — равен "
            "`toolCall.id` из ответа `/v1/chat/run`."
        ),
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="DEPRECATED (одиночная форма). Результат исполнения инструмента.",
    )
    error: ToolErrorBody | None = Field(
        default=None,
        description="DEPRECATED (одиночная форма). Ошибка исполнения инструмента.",
    )

    @model_validator(mode="after")
    def _check(self) -> ChatToolResultRequest:
        has_batch = self.results is not None
        has_single = (
            self.toolCallId is not None or self.result is not None or self.error is not None
        )
        if has_batch == has_single:
            raise ValueError(
                "exactly one of batch form (results) or single form "
                "(toolCallId + result/error) is required"
            )
        if has_batch:
            if not self.results:
                raise ValueError("results must be a non-empty list")
            seen: set[uuid.UUID] = set()
            for item in self.results:
                # Duplicate toolCallId within one batch → 422 (ADR-025 idempotency rules).
                if item.toolCallId in seen:
                    raise ValueError("duplicate toolCallId in batch")
                seen.add(item.toolCallId)
            return self
        # Single (deprecated) form: validate exactly one of result/error + size.
        if self.toolCallId is None:
            raise ValueError("toolCallId is required in single form")
        if (self.result is None) == (self.error is None):
            raise ValueError("exactly one of result/error is required")
        if self.result is not None:
            import json

            settings = get_settings()
            if len(json.dumps(self.result).encode("utf-8")) > settings.size_limit_tool_result:
                raise ValueError("result exceeds size limit")
        return self

    def normalized_results(self) -> list[ToolResultItem]:
        """Normalize to a batch list (ADR-025): single form → list of one. Order preserved."""
        if self.results is not None:
            return self.results
        assert self.toolCallId is not None  # noqa: S101 - guaranteed by _check
        return [ToolResultItem(toolCallId=self.toolCallId, result=self.result, error=self.error)]


class ToolCallSchema(StrictModel):
    id: str = Field(
        description="Идентификатор вызова инструмента. Возвращается клиентом в `toolCallId`."
    )
    name: str = Field(
        description="Имя инструмента для исполнения на устройстве (например, `files.read`)."
    )
    args: dict[str, Any] = Field(description="Аргументы вызова инструмента.")


class ServerToolExecutionSchema(StrictModel):
    """Одно server-side выполнение, выполненное backend за этот вызов /chat/run."""

    toolCallId: str = Field(
        description=(
            "Доменный идентификатор вызова инструмента (uuid4) этого server-side выполнения. "
            "Совпадает с `toolCallId` соответствующего tool-шага в `GET /v1/chats/{id}` → "
            "`steps[].payload.toolCallId` (нормативный инвариант корреляции). Тот же домен id, "
            "что у client-side `toolCalls[].id`; НЕ provider-`toolu_...`."
        )
    )
    toolName: str = Field(
        description=(
            "Доменное имя инструмента с точкой (например `time.now`, `site.write_file`). "
            "Совпадает с `name` из `/v1/tools` и `toolName` из `GET /v1/chats/{id}/steps`."
        )
    )
    status: Literal["completed", "errored"] = Field(
        description=(
            "Итог выполнения: `completed` (успех) или `errored` (инструмент вернул ошибку; ход "
            "при этом не падает)."
        )
    )
    summary: str | None = Field(
        default=None,
        description=(
            "Компактный человекочитаемый итог (≤120 символов). НЕ raw-результат: без путей, URL, "
            "имён превью-файлов со signed-token и иных чувствительных данных. Полный результат — "
            "только в истории `GET /v1/chats/{id}`."
        ),
    )


class QuizSchema(StrictModel):
    """Квиз режима Study & Learn ([ADR-057](adr/ADR-057-study-learn-quiz-contract.md)).

    Результат global server-side инструмента `quiz.generate`, поднятый в `ChatResponse.quiz`.
    `options` — 2..10 вариантов; `correctIndex` — 0-based индекс правильного варианта; проверка
    ответа выполняется на клиенте (`correctIndex` уходит клиенту — осознанный компромисс, ADR-057
    §6). Валидатор `correctIndex < len(options)` дублирует инвариант `QuizGenerateArgs` на границе
    ответа (защита от рассинхрона).
    """

    question: str = Field(description="Текст вопроса квиза.")
    options: list[str] = Field(
        min_length=2,
        max_length=10,
        description="Варианты ответа (2..10). Индекс правильного — в `correctIndex`.",
    )
    correctIndex: int = Field(
        ge=0,
        description="0-based индекс правильного варианта в `options` (обязан быть < len options).",
    )
    explanation: str = Field(description="Пояснение, почему правильный вариант верен.")

    @model_validator(mode="after")
    def _check_correct_index(self) -> QuizSchema:
        if self.correctIndex >= len(self.options):
            raise ValueError("correctIndex must be within the options range")
        return self


class GeneratedImageRefSchema(StrictModel):
    """Ссылка на сгенерированное изображение ([ADR-058](adr/ADR-058-image-generation.md)).

    Возвращается в `ChatResponse.images` при вызове server-side инструмента `image.generate`.
    Несёт ТОЛЬКО идентификатор и метаданные — байты фетчатся отдельно `GET /v1/images/{imageId}`
    под JWT (чужое → 404). Промт НЕ включается (TD-035: не логируется и не отдаётся в метаданных).
    """

    imageId: uuid.UUID = Field(
        description="Идентификатор изображения для `GET /v1/images/{imageId}` (байты под JWT)."
    )
    contentType: str = Field(
        description="MIME изображения (`image/png` | `image/jpeg` | `image/webp`)."
    )
    size: int = Field(ge=0, description="Размер изображения в байтах.")


_BLOCK_REASON_DOC = (
    "Причина бизнес-блокировки (присутствует только при `status=blocked`). Значения:\n\n"
    "- `trial_used` — бесплатная пробная генерация использована, подписки нет. "
    "UI: предложить оформить подписку.\n"
    "- `subscription_required` — действие требует активной подписки, её нет. "
    "UI: экран оформления подписки.\n"
    "- `subscription_expired` — подписка была, но истекла/отозвана. UI: предложить продлить.\n"
    "- `credits_empty` — баланс кредитов исчерпан (режим `credits`). "
    "UI: показать баланс, предложить пополнение/подписку.\n"
    "- `byok_disabled` — режим `byok` выбран, но BYOK выключен пользователем. UI: включить BYOK.\n"
    "- `byok_invalid` — ключ BYOK отсутствует или невалиден. UI: добавить/исправить ключ.\n"
    "- `rate_limited` — мягкое превышение лимита оркестрации (жёсткое — `429`). "
    "UI: «слишком часто», предложить повторить позже.\n"
    "- `policy_denied` — общий fallback для непредвиденного состояния Policy Engine. "
    "UI: generic-сообщение «недоступно», лог/ретрай.\n"
    "- `max_tokens` — ответ модели обрезан лимитом output-токенов. В отличие от прочих "
    "причин — `usage`/`messageStepId`/`stepId` присутствуют, кредит не списан, `toolCall(s)` "
    "не отдаются. UI: повторить/сократить запрос.\n"
    "- `image_credits_empty` — кредитов не хватило на генерацию изображения (`image.generate`, "
    "режим `credits`). Байты не сохранены, дебет откатан (ADR-058 §5). Отдельно от "
    "`credits_empty` — UI показывает профильный CTA «недостаточно кредитов на изображение». "
    "Не policy-причина (как `max_tokens`): `evaluate()` её не возвращает, в "
    "`/policy/effective.reasons[]` не входит."
)


class ChatResponse(StrictModel):
    """Ответ chat-endpoint: три взаимоисключающих состояния по полю `status`.

    - `status=assistant_message`: есть `assistantMessage`, `usage`; нет `toolCall(s)`,
      `blockReason`.
    - `status=tool_call`: есть `toolCalls[]` (все client-side вызовы хода) и `toolCall`
      (= `toolCalls[0]`, deprecated), `usage`; `assistantMessage` опционален — присутствует, если
      модель выдала текст вместе с tool_use (текст того же шага); нет `blockReason`.
    - `status=blocked`: есть `blockReason`; нет `toolCall(s)`. При `blockReason=max_tokens`
      `usage`/`messageStepId`/`stepId`/`assistantMessage` присутствуют (ход обрезан после
      начала генерации); при policy-blocked все они `null`.

    `messageStepId`/`stepId` присутствуют при `assistant_message`/`tool_call` и при
    `blocked`+`max_tokens`; `null` при policy-`blocked` (шаг/ход не создаются).

    `serverTools` — server-side выполнения (`site.*`/`time.now`) этого вызова; всегда
    присутствует (возможно `[]`). Пустой при policy-`blocked`; может быть НЕпустым при
    `blocked`+`max_tokens`.

    `quiz` — ортогонален `status` (ADR-057): присутствует (не `null`) только когда
    `dialogMode=study_learn` и модель вызвала `quiz.generate`; иначе `null`/опущено. Обычно
    сопровождает финальный `assistant_message` (квиз + пояснительный текст за один ход), но может
    прийти и на `tool_call` (квиз выполнен до client-side инструмента) или на `blocked`+`max_tokens`
    (раунд квиза успел отработать до обрыва). При policy-`blocked` — `null`.
    """

    status: Literal["assistant_message", "tool_call", "blocked"] = Field(
        description="Состояние ответа: `assistant_message` | `tool_call` | `blocked`.",
    )
    sessionId: uuid.UUID = Field(
        description="Идентификатор сессии диалога (для последующих вызовов)."
    )
    messageStepId: uuid.UUID | None = Field(
        default=None,
        description=(
            "Идентификатор хода для синхронизации с историей чата. Один на сообщение, "
            "общий для всех раундов tool-loop. Совпадает с `messageStepId` шагов хода в "
            "`GET /v1/chats/{id}`. `null` при `status=blocked`."
        ),
    )
    stepId: uuid.UUID | None = Field(
        default=None,
        description=(
            "Идентификатор конкретного шага, который представляет этот ответ. Совпадает с "
            "`id` соответствующего шага в `GET /v1/chats/{id}`. `null` при `status=blocked`."
        ),
    )
    assistantMessage: str | None = Field(
        default=None,
        description=(
            "Текст ответа ассистента. При `status=assistant_message` — финальный ответ. При "
            "`status=tool_call` — опционально текст того же шага, если модель выдала его вместе "
            "с вызовом инструмента (иначе `null`). При `status=blocked` — `null`."
        ),
    )
    toolCalls: list[ToolCallSchema] | None = Field(
        default=None,
        description=(
            "ВСЕ client-side вызовы инструментов текущего хода (parallel tool use). "
            "Присутствует только при `status=tool_call`. Клиент обязан исполнить и вернуть "
            "результаты на все элементы через `/v1/chat/tool-result`. Server-side `site.*` сюда "
            "не входят."
        ),
    )
    toolCall: ToolCallSchema | None = Field(
        default=None,
        description=(
            "DEPRECATED (читайте `toolCalls[]`). Первый client-side вызов хода (= `toolCalls[0]`). "
            "Присутствует при `status=tool_call`. На мульти-tool ходе неполон — continuation "
            "сломается, если читать только его."
        ),
    )
    blockReason: str | None = Field(default=None, description=_BLOCK_REASON_DOC)
    usage: dict[str, Any] | None = Field(
        default=None,
        description="Потребление токенов модели (при `assistant_message`/`tool_call`).",
    )
    serverTools: list[ServerToolExecutionSchema] = Field(
        default_factory=list,
        description=(
            "Server-side инструменты (`site.*`, `time.now`), выполненные backend за ЭТОТ вызов "
            "`/chat/run` (или один `/chat/tool-result`-continuation), в порядке выполнения. "
            "Присутствует всегда: пустой `[]` — server-side не выполнялись (в т.ч. "
            "policy-`blocked`, где tool-loop не запускался); при `blocked=max_tokens` может быть "
            "НЕпустым (раунды до обрыва). client-side вызовы здесь НЕ перечисляются — они в "
            "`toolCalls[]`. Информационное поле: на биллинг не влияет."
        ),
    )
    quiz: QuizSchema | None = Field(
        default=None,
        description=(
            "Квиз режима Study & Learn ([ADR-057]). Присутствует только когда "
            "`dialogMode=study_learn` и модель вызвала `quiz.generate`; иначе `null`/опущено. "
            "`options` — 2..10 вариантов; `correctIndex` — 0-based индекс правильного (проверка "
            "ответа — на клиенте). Ортогонален `status`."
        ),
    )
    images: list[GeneratedImageRefSchema] | None = Field(
        default=None,
        description=(
            "Изображения, сгенерированные `image.generate` в этом ходе ([ADR-058]). Каждый "
            "элемент — только `imageId`/`contentType`/`size`; байты фетчатся "
            "`GET /v1/images/{imageId}` под JWT (чужое → 404). Каждое изображение = отдельный "
            "дебет `IMAGE_CREDITS_COST` сверх кредита за ход (итог `1 + N×COST`). `null`/опущено, "
            "если картинок не было. Нехватка кредитов на картинку → `status=blocked`, "
            "`blockReason=image_credits_empty` (байты не сохранены)."
        ),
    )
