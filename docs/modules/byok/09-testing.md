# BYOK — Testing

## Unit
- AES-256-GCM encrypt→decrypt round-trip восстанавливает ключ.
- KmsClient (fake): encrypt_dek→decrypt_dek round-trip.
- Tampered ciphertext/tag → ошибка аутентификации GCM.

## Integration (respx для Anthropic, fake KMS, AC-5)
- `set` валидным ключом → keyStatus=valid; в БД хранится только ciphertext (нет plaintext).
- `set` невалидным ключом → keyStatus=invalid, не enabled.
- Логи/audit НЕ содержат plaintext ключ (assert по redaction).
- `toggle enabled=true` при invalid → не включается.
- `delete` → строка удалена, keyStatus=missing.
- `get_plaintext_key` восстанавливает исходный ключ (через fake KMS).
- Ответы endpoint не содержат ключ.
