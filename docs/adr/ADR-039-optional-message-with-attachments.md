# ADR-039: Опциональный `message` в `/v1/chat/run` при наличии вложений (image-only / file-only ход)

- Статус: Accepted
- Дата: 2026-06-18
- Связано: [ADR-020](ADR-020-inline-base64-attachments-mvp.md) (inline base64-вложения), [ADR-033](ADR-033-llm-provider-abstraction.md) (провайдер-абстракция), [ADR-037](ADR-037-chatrunrequest-context-allowlist-injection.md) (context-блок в turn-0 user-сообщении), [ADR-036](ADR-036-workspaces-implementation.md) (workspace-файлы первого хода), [ADR-006](ADR-006-credit-billing-and-subscription-grant.md) (биллинг)

## Контекст

iOS-репорт (прод broadnova): отправка `POST /v1/chat/run` с изображением и **пустым** `message` отклоняется как `422 request validation failed`.

Причина — контракт требует непустой текст: `ChatRunRequest.message: str = Field(min_length=1)` (`src/app/schemas/chat.py:88`). Пользовательский сценарий «отправить только фото/файл без подписи» (распознать картинку, разобрать документ) невозможен, хотя оба провайдера (Anthropic, OpenAI) принимают user-сообщение, состоящее только из image/file-блока без текста.

Текущая сборка turn-0 user-сообщения (`src/app/chat/orchestrator.py:535-544`) всегда добавляет text-блок:

```python
context_block = _render_context_block(context)
message_text = f"{context_block}\n\n{message}" if context_block is not None else message
user_payload_content = [{"type": "text", "text": message_text}]
if attachments:
    prepared = prepare_attachments(...)
    user_payload_content = [{"type": "text", "text": message_text}, *prepared.placeholders]
```

При пустом `message` (после снятия `min_length=1`) это даст text-блок с `text=""` (или с висячим `"\n\n"` от context-блока). **Пустой text-блок может быть отвергнут провайдером** (Anthropic/OpenAI ругаются на `text=""`), поэтому простого снятия `min_length` недостаточно — нужно не добавлять text-блок при пустом тексте.

docs↔код согласованы (дока `02-api-contracts.md` и `API-REFERENCE.md` описывают `message` как обязательный, код это и реализует) — расхождения нет, blocked не требуется.

## Решение

Разрешить пустой `message`, **если есть хотя бы одно вложение**. Текст становится опциональным; ход валиден при «непустой текст ИЛИ ≥1 вложение».

### §1. Валидация схемы (`ChatRunRequest`)

1. Поле: `message: str = Field(default="", description=...)` — снять `min_length=1`, дефолт `""` (пустая строка). Тип остаётся `str` (не `str | None`) — отсутствие поля = пустой текст, единая семантика «нет текста».
2. В `model_validator` `_check_sizes` добавить правило **«message или ≥1 attachment»**:
   - валидно, если `self.message.strip()` непуст **ИЛИ** `self.attachments` непуст (`is not None and len >= 1`);
   - иначе → `ValueError("message or at least one attachment is required")` (→ `422`).
3. **Size-лимит `message` сохраняется** (`len(self.message.encode("utf-8")) > settings.size_limit_message` → `422`). Для пустой строки тривиально проходит.
4. Прочие проверки (`projectId`/`model` непустые при наличии, `context` size) — без изменений.

`attachments` валидируются `prepare_attachments` уже в orchestrator (ADR-020); схема проверяет только **наличие** ≥1 элемента, не их содержимое.

### §2. Сборка turn-0 user-сообщения (orchestrator)

Правило: **text-блок добавляется в user-content только если итоговый текст непуст.** При пустом тексте отправляются только attachment-блоки (vision/document/text-file) — провайдер не получает пустой text-блок (§4).

«Итоговый текст» = результат склейки `message` с context-блоком ADR-037 (§3). Пустым он считается, когда **строка пуста** (`text == ""`); это исчерпывающе покрывает «нет message и нет context-блока». Если message пуст, но context-блок есть — итоговый текст = сам блок (непустой) → text-блок добавляется как обычно.

Псевдокод (заменяет `orchestrator.py:535-544`):

```python
context_block = _render_context_block(context)
message_text = _compose_turn0_text(context_block, message)   # см. §3
prepared = prepare_attachments(...) if attachments else None
text_blocks = [{"type": "text", "text": message_text}] if message_text else []
placeholders = prepared.placeholders if prepared else []
user_payload_content = [*text_blocks, *placeholders]
```

Инвариант: `user_payload_content` непуст всегда (гарантировано §1: пустой text ⇒ есть ≥1 attachment ⇒ есть placeholder). Порядок: text-блок (если есть) **первым**, затем attachment-плейсхолдеры — как сейчас.

`first_turn = _merge_attachments(prepared, workspace_attachments)` (ADR-036) — без изменений; workspace-файлы по-прежнему добавляются перед request-вложениями в content-блоках, передаваемых клиенту. (Замечание: workspace-файлы — самостоятельные attachment-блоки и сами по себе НЕ удовлетворяют требование §1 «≥1 attachment» — оно про `attachments` запроса; см. §6 Edge.)

### §3. Склейка с context-блоком ADR-037

Точная матрица (`block` = `_render_context_block(context)`, `msg` = `message`):

| message | context-блок | Итоговый text | text-блок в user-content |
|---|---|---|---|
| непустой | есть | `block + "\n\n" + msg` | да (как сейчас) |
| непустой | нет (`None`) | `msg` | да (как сейчас) |
| пустой (`""`/whitespace) | есть | `block` (**без** висячего `"\n\n"`) | да |
| пустой (`""`/whitespace) | нет (`None`) | `""` | **нет** (только attachment-блоки) |

Helper `_compose_turn0_text(block, msg)`:

```python
def _compose_turn0_text(block: str | None, msg: str) -> str:
    if not msg:                      # пустой/пустой-после-неявного дефолта текст
        return block or ""           # context-блок или вовсе пусто
    if block is not None:
        return f"{block}\n\n{msg}"   # как сейчас
    return msg                       # как сейчас
```

Висячий `"\n\n"` при пустом message и наличии блока **не** появляется. Когда оба пусты — `""` → §2 не создаёт text-блок.

Семантика «пустой message»: для **сборки** (§2/§3) пустым считается `message == ""` (raw, как пришёл). Используется именно raw-`message`, а не `.strip()` — пробелы внутри легитимного текста не трогаем; «whitespace-only» уже отсечён валидатором §1 (там `.strip()`), и сюда whitespace-only без вложений не дойдёт. При наличии вложений whitespace-only message сохраняется в text как есть (минорно; не нарушает провайдер, текст непуст). Чтобы whitespace-only с вложениями не порождал почти-пустой text-блок, рекомендуется в §2 нормализовать: трактовать `message.strip() == ""` как «нет текста» — то есть в `_compose_turn0_text` условие `if not msg` заменить на `if not msg.strip()`. **Принятое решение: использовать `not msg.strip()`** (символическая разница от §1, где тоже `.strip()`), чтобы не слать провайдеру блок из одних пробелов.

### §4. Провайдер-агностичность

Пустой text-блок (`text=""`) **не отправляется ни одному провайдеру** — §2 его не создаёт. Image-only / file-only user-turn:
- **Anthropic** — user-message из одного `image`/`document` блока валиден;
- **OpenAI** (ADR-033) — `image_url`-блок без текстового блока валиден; PDF на OpenAI по-прежнему `422` (TD-023) — не меняется *(снято [ADR-041](ADR-041-openai-native-pdf-attachment.md): PDF на OpenAI теперь поддержан, TD-023 Resolved; ослабление обязательности `message` из ADR-039 не затронуто)*.

Маппинг вложений в content-блоки (`attachments.py`) — без изменений. Решение про «не слать пустой text» реализуется в orchestrator (единая turn-0 сборка), а не в клиентах.

### §5. Биллинг и хранение

- Биллинг неизменен (ADR-006): ход = 1 кредит = 1 завершённое сообщение, независимо от наличия текста.
- Хранение/replay (ADR-020 §3): персистится `user_payload_content` (text-блок при наличии + плейсхолдеры). При пустом тексте в `chat_steps.payload` сохраняются только плейсхолдеры → корректный replay (`_build_messages` поднимает `payload["content"]` как есть).
- Авто-заголовок: `derive_title(message)` уже возвращает `None` для пустого/whitespace-only текста (`repository.py:41-43`) → список чатов корректно фолбэчит на preview. Поведение менять не нужно.
- Миграции БД **нет** (контрактное изменение схемы запроса).

### §6. Edge-кейсы

- **Только текстовое файл-вложение** (`type: text`/`document`) без `message` — валидно (§1: ≥1 attachment). Маппится в text-content-блок вложения (`<filename>\n```\n<содержимое>\n````), а пользовательский text-блок отсутствует.
- **Пустой message + только workspace-файлы (ADR-036), без `attachments` запроса** — **`422`** (требование §1 — про `attachments` **запроса**; workspace-файлы автоматически контекст не делают ход «с вложением»). Workspace-контекст подаётся вместе с пользовательским вводом, а не вместо него; для голого «продолжи по файлам проекта» клиент должен прислать хотя бы текст. Симметрично iOS-UX (отправка возможна при наличии текста ИЛИ вложения в композере).
- **Пустой message + пустые attachments (`[]` или отсутствуют)** — `422` «message or at least one attachment is required».
- **whitespace-only message + ≥1 attachment** — валидно (§1: есть attachment); text-блок не создаётся (§3, `not msg.strip()`).

### §7. Обратная совместимость

- Непустой `message` без вложений — без изменений (text-блок как сейчас).
- Непустой `message` + вложения — без изменений.
- Контракт **расширяется** (ослабляется обязательность поля) — это не breaking change: все ранее валидные запросы остаются валидными. Новые ранее-`422` запросы (image-only) теперь проходят.

## Последствия

Плюсы:
- iOS может отправлять фото/файл без подписи — закрывает прод-репорт.
- Изменение локально: одно поле схемы + один валидатор + одна точка сборки turn-0.
- Провайдер-агностично, без миграций, без влияния на биллинг.

Минусы / риски:
- Появляется ещё одно условное ветвление в turn-0 сборке (text-блок опционален). Митигировано helper'ом `_compose_turn0_text` и инвариантом «user-content непуст».
- На OpenAI image-only работает; PDF-only по-прежнему `422` (TD-023) — клиент OpenAI-инстанса не должен слать PDF (уже задокументировано) *(снято [ADR-041](ADR-041-openai-native-pdf-attachment.md): PDF-only ход теперь валиден и на OpenAI, TD-023 Resolved)*.

## Альтернативы

1. **`message: str | None = None`** вместо `str = ""`. Отклонено: добавляет `None`-ветвления по коду (`.encode`, `derive_title`, склейка); `str=""` даёт единую семантику «нет текста» с меньшим diff.
2. **Слать провайдеру пустой text-блок (`text=""`)** при пустом message. Отклонено: провайдеры могут отвергать пустой text-блок (риск `400→502`); §2 явно его не создаёт.
3. **Подставлять плейсхолдер-текст** (напр. `"[image]"`) при пустом message. Отклонено: засоряет контекст/историю синтетическим текстом, влияет на ответ модели; чистый image-only ход предпочтительнее.
4. **Разрешать пустой message всегда** (даже без вложений). Отклонено: пустой ход без контента бессмысленен и тратил бы кредит; правило «текст ИЛИ вложение» отсекает.
