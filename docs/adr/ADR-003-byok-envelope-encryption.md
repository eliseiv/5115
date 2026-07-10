# ADR-003 — BYOK: envelope encryption (AES-256-GCM + KMS)

- Статус: Accepted
- Дата: 2026-05-21
- Update (MVP, 2026-06-02): дефолтная **и** prod-реализация `KmsClient` = `LocalKmsClient` (master key из env/secret manager). Облачный KMS — post-MVP через тот же интерфейс ([Q-002-1](../99-open-questions.md)). Ядро решения (envelope encryption, AES-256-GCM, единый интерфейс `KmsClient`) не меняется.
- Update (2026-06-25, [ADR-044](ADR-044-multi-provider-byok.md), мульти-провайдерный BYOK): envelope-схема (`set`/`get_plaintext_key`) **не меняется**. Шаг 6 валидации (лёгкий вызов «под ключом») выполняется клиентом провайдера, **определённого по самому ключу** (детектор префиксов), а не провайдера инстанса. Plaintext-ключ по-прежнему расшифровывается **только** на генерации; новая колонка `byok_keys.provider` (миграция `0013`) хранит провайдера, чтобы отдавать `activeModel`/статус **без** расшифровки ключа на чтениях `/v1/byok` (минимизация поверхности plaintext).

## Context
Пользователи могут предоставить собственный Anthropic API key (BYOK). Требование: шифрование at-rest (KMS/эквивалент), ключ никогда не логируется и не утекает (AC-5, [05-security.md](../05-security.md)). Прямое шифрование каждого ключа вызовом KMS на чтение даёт KMS-вызов на каждую генерацию — латентность и стоимость.

## Decision
**Envelope encryption**:
1. На `set`: генерируем случайный **DEK** (32 байта, CSPRNG).
2. Шифруем пользовательский ключ **AES-256-GCM** с DEK → `encrypted_key` + `nonce` (+ auth tag).
3. Шифруем DEK через **KMS** master key → `encrypted_dek`.
4. Храним в `byok_keys`: `encrypted_key`, `encrypted_dek`, `nonce`. **Plaintext ключ и plaintext DEK не хранятся.**
5. На использование (`mode=byok`): KMS `Decrypt(encrypted_dek)` → DEK in-memory → AES-GCM decrypt → plaintext ключ передаётся **только** Chat Orchestrator на время одного вызова Anthropic, затем обнуляется.
6. Валидация при `set`: лёгкий вызов Anthropic под ключом → `key_status = valid | invalid`.

Стабильный интерфейс `KmsClient(encrypt_dek, decrypt_dek)` с двумя реализациями:
- **`LocalKmsClient`** — реальный AES-256-GCM wrap DEK под master key из env/secret manager (`KMS_LOCAL_MASTER_KEY`). **Дефолт на MVP и в prod** (решение пользователя 2026-06-02). Рабочая envelope-схема, не заглушка: DEK никогда не хранится в plaintext.
- **Облачный KMS** — post-MVP, подключается в тот же интерфейс `KmsClient` без изменения контрактов; конкретный провайдер — [Q-002-1](../99-open-questions.md).

AES-GCM реализуется через `cryptography`.

## Consequences
- (+) Один master key в KMS, ротация на уровне KMS без перешифровки всех данных.
- (+) Минимум KMS-вызовов (только DEK), AES-GCM локально — низкая латентность.
- (+) AEAD (GCM) защищает целостность ciphertext.
- (−) Кэширование plaintext DEK для скорости не делаем на старте (TD при необходимости) — KMS Decrypt на каждое использование byok.
- (−) Зависимость от доступности KMS для генерации в режиме byok.

## Alternatives
- Прямое KMS-шифрование самого ключа (без DEK) — отвергнуто: KMS-вызов на каждую генерацию, лимиты/стоимость.
- Симметричный app-level ключ из env без KMS — отвергнуто: ключ шифрования живёт рядом с данными, слабее модель угроз.
- Хранить ключ в внешнем secret manager per-user — отвергнуто: оверхед на количество секретов, сложнее delete/toggle.
