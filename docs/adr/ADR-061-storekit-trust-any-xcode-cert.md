# ADR-061 — StoreKit: доверие любому локальному сертификату Xcode StoreKit Testing (`STOREKIT_TRUST_ANY_XCODE_CERT`)

- **Статус:** Accepted
- **Дата:** 2026-07-13
- **Контекст-долг:** [TD-039](../100-known-tech-debt.md)
- **Соседствует с:** [TD-007](../100-known-tech-debt.md) (`STOREKIT_TEST_MODE`, HS256 test-mode), [Q-007-1](../99-open-questions.md) (posture sandbox/prod)
- **Затрагивает код:** `src/app/subscription/storekit.py` (`StoreKitVerifier._verify_real_transaction`), `src/app/config.py` (`Settings`)

## Контекст

`StoreKitVerifier` (`src/app/subscription/storekit.py`) верифицирует подписанную JWS-транзакцию
App Store двумя путями (ветвление по `alg` в `verify()`):

- `alg=HS256` → **test-mode** (`_verify_test_transaction`), активен только при
  `STOREKIT_TEST_MODE=true` + непустой `STOREKIT_TEST_SECRET` ([TD-007](../100-known-tech-debt.md)).
- иначе (`ES256` + `x5c`) → **реальный путь** `_verify_real_transaction`: `_load_certificate_chain()`
  → `leaf = chain[0]`; если `self._roots` пуст → `ValidationFailedError` («App Store root
  certificates not configured …»); иначе `_verify_chain(chain, self._roots)` (попарная проверка
  подписи в цепочке + требование, что корень цепочки есть среди `roots` по DER **или** подписан
  одним из `roots`, иначе `ValidationFailedError("StoreKit certificate chain not anchored to a
  trusted root")`); затем проверка ES256-подписи публичным ключом `leaf` через
  `jwt.decode(..., key=leaf_pubkey, algorithms=["ES256"])`; затем `_normalize_payload()`.

**Проблема.** На пред-релизном тестовом инстансе нужно принимать транзакции, сгенерированные
**локальным StoreKit Testing в Xcode** с произвольной машины разработчика/тестировщика. Такая
транзакция подписана `ES256` с `x5c` (значит, идёт реальным путём, а не HS256 test-mode), но её
сертификат — **самоподписанный локальный серт Xcode** (subject `O=StoreKit Testing in Xcode,
CN=StoreKit Testing in Xcode`), а не выданный Apple. Реальный путь отклоняет её на **двух** гейтах:
(1) `if not self._roots` — на тестовом инстансе Apple root CA не сконфигурирован
([Q-007-1](../99-open-questions.md) не закрыт); (2) `_verify_chain` — самоподписанный серт не
заякорен в Apple root. `STOREKIT_TEST_MODE` (HS256) эту потребность не покрывает: там нужен общий
секрет и специально сформированный HS256-токен, а Xcode отдаёт настоящий ES256-JWS.

**Осознанный риск.** Приложение ещё не в проде, реальных пользователей нет; владелец сервиса явно
принял риск доверия любому серту с фиксированным Xcode-CN на **тестовом** инстансе, чтобы прогонять
активацию подписки + начисление кредитов с реальным StoreKit Testing-потоком iOS-клиента.

## Решение

Вводится новый env-флаг `STOREKIT_TRUST_ANY_XCODE_CERT` (bool), **дефолт `false`** (fail-closed:
прод и любой инстанс без флага не затронуты). Нормативные требования:

1. **Новый конфиг-флаг.** `Settings.storekit_trust_any_xcode_cert: bool = Field(default=False,
   alias="STOREKIT_TRUST_ANY_XCODE_CERT")` в блоке StoreKit `src/app/config.py`. Имя финализировано:
   `STOREKIT_TRUST_ANY_XCODE_CERT` — точно описывает эффект (доверие ЛЮБОМУ Xcode-серту), симметрично
   стилю `STOREKIT_TEST_MODE`/`APPLE_TEST_MODE`. Дефолт `false` следует проектному паттерну
   «malformed / не задано → безопасный дефолт»: отсутствующий или невалидный env НЕ ослабляет
   верификацию.

2. **Фиксированный CN.** Признак локального серта Xcode — **subject Common Name листового
   сертификата** (`chain[0]`), равный строке **`StoreKit Testing in Xcode`** (полный subject
   `O=StoreKit Testing in Xcode, CN=StoreKit Testing in Xcode`; сравнивается именно CN). Сравнение —
   точное равенство строки CN. CN извлекается из `leaf.subject`
   (`x509.NameOID.COMMON_NAME`); отсутствие/множественность CN → **не совпадение** → обычный путь.

3. **Нормативное поведение (ES256-путь, `_verify_real_transaction`).** КОГДА
   `STOREKIT_TRUST_ANY_XCODE_CERT=true` **И** CN листа == `"StoreKit Testing in Xcode"` — транзакция
   принимается **без** требования, чтобы её корень был доверенным: пропускаются **оба** гейта
   заякоривания — и `if not self._roots` («root certificates not configured»), и вызов
   `_verify_chain(chain, self._roots)`. **НО** ES256-подпись листа проверяется штатно
   (`jwt.decode(..., key=leaf_pubkey, algorithms=["ES256"])`): токен обязан быть подписан
   **предъявленным** сертификатом, иначе `ValidationFailedError` («StoreKit JWS signature invalid»).
   Далее — обычный `_normalize_payload()` (сверка `bundleId`/`environment`, извлечение полей). Так
   принимается локальная транзакция с ЛЮБОЙ машины Xcode, но токен остаётся внутренне-целостным
   (нельзя подделать payload, не имея приватного ключа предъявленного серта).

4. **Граница: флаг `false` (дефолт) — поведение НЕ меняется ни на йоту.** При
   `STOREKIT_TRUST_ANY_XCODE_CERT=false` (и при незаданном env) `_verify_real_transaction`
   выполняется буква-в-букву как сейчас: пустые `roots` → `ValidationFailedError`;
   `_verify_chain` с требованием заякоривания; ES256-подпись листа. Fail-closed сохраняется.

5. **Граница: реальный Apple-путь НЕ ослабляется.** Флаг только **ДОБАВЛЯЕТ** приём самоподписанных
   сертов с Xcode-CN; он **не убирает** существующую проверку для остальных сертов. Настоящие
   sandbox/production Apple-транзакции (CN ≠ `StoreKit Testing in Xcode`) продолжают проходить
   `_verify_chain` против Apple root CA штатно даже при `STOREKIT_TRUST_ANY_XCODE_CERT=true`. Обход
   гейтов заякоривания применяется **исключительно** к ветке «флаг on И CN совпал».

6. **Граница: флаг on, но CN не совпал → обычный путь.** Если `STOREKIT_TRUST_ANY_XCODE_CERT=true`,
   но CN листа ≠ `"StoreKit Testing in Xcode"` — идёт штатный реальный путь со всеми гейтами
   (никакого ослабления). Флаг не является «выключателем верификации», это узкий allow конкретного
   Xcode-CN.

7. **Граница: HS256 test-mode — отдельная ветка, флаг её не касается.** `STOREKIT_TRUST_ANY_XCODE_CERT`
   не влияет на ветку `alg=HS256` (`_verify_test_transaction`, [TD-007](../100-known-tech-debt.md)) и
   не заменяет её. Флаги ортогональны: `STOREKIT_TEST_MODE` управляет HS256-путём,
   `STOREKIT_TRUST_ANY_XCODE_CERT` — заякориванием на ES256-пути.

8. **Цепочка из одного серта (самоподписанный).** Локальный Xcode-серт самоподписан: цепочка `x5c`
   состоит из одного сертификата, `leaf == chain[0] == chain[-1]`. Поэтому обход применяется к CN
   **листа** (`chain[0]`). Попарная проверка подписи `_verify_chain` для одного серта — пустой цикл;
   единственное, что делает `_verify_chain` сверх неё, — проверка заякоривания, которую §3 и
   пропускает. Поэтому для доверенного Xcode-случая `_verify_chain` пропускается целиком (нечего
   попарно проверять), а целостность гарантируется ES256-подписью листа (§3). Если фактическая
   транзакция вдруг содержит цепочку > 1 серта — backend обязан свериться на реальном транзакции
   (см. ТЗ), но нормативный критерий остаётся: CN **листа** `chain[0]`.

9. **Наблюдаемость (защита от случайного включения).** При старте приложения, если
   `STOREKIT_TRUST_ANY_XCODE_CERT=true`, писать **WARNING в лог** (образец
   `STOREKIT_TEST_MODE`/[TD-007](../100-known-tech-debt.md)): предупреждение, что принимаются любые
   самоподписанные Xcode-серты и флаг ДОЛЖЕН быть `false` в проде. Payload/токен не логируются
   ([05-security.md](../05-security.md)).

## Последствия

- **Плюс:** пред-релизный тестовый инстанс принимает реальные ES256-транзакции локального StoreKit
  Testing с любой машины Xcode без Apple root CA и без специального HS256-секрета — сквозной прогон
  активации подписки + начисления кредитов ([ADR-006](ADR-006-credit-billing-and-subscription-grant.md))
  с настоящим iOS-клиентом.
- **Минус / риск:** при `STOREKIT_TRUST_ANY_XCODE_CERT=true` любой, кто предъявит валидно
  самоподписанный серт с CN `StoreKit Testing in Xcode` и подпишет им токен, пройдёт верификацию.
  Это осознанный, документированный долг ([TD-039](../100-known-tech-debt.md)): **только** для
  пред-релизного тестового инстанса, **ОТКЛЮЧИТЬ перед продакшеном**. ES256-подпись листа
  по-прежнему проверяется (payload не подделать без ключа предъявленного серта), но доверие к
  происхождению серта снимается — поэтому флаг не должен доживать до реальных пользователей.
- **Нейтрально:** дефолт `false` → прод и все существующие инстансы не затронуты; при незаданном env
  верификация fail-closed как сейчас.

## Альтернативы (отклонены)

- **Расширить `STOREKIT_TEST_MODE` (HS256-путь).** Отклонено: Xcode отдаёт настоящий ES256-JWS с
  `x5c`, а не HS256-токен под общим секретом. Смешивать два разных механизма в одном флаге —
  запутать семантику и риск-модель; ортогональные флаги (§7) чище.
- **Загрузить Xcode-серт в `APPSTORE_ROOT_CERT_DIR` как доверенный root.** Отклонено: серт локального
  Xcode **свой на каждой машине** (разные ключи), пришлось бы собирать и обновлять серты всех
  разработчиков/тестировщиков; «любой Xcode-CN» покрывает произвольную машину одним флагом.
- **Полностью отключить верификацию цепочки при флаге.** Отклонено: сняло бы и ES256-проверку подписи
  листа → приём любого payload без криптозащиты. §3 сохраняет подпись листа — токен остаётся
  внутренне-целостным.
- **Новый ADR vs расширение существующего.** Выделенного ADR по верификации сертификата StoreKit
  нет: реальный путь описан в [modules/subscription/03-architecture.md](../modules/subscription/03-architecture.md)
  + [09-e2e-testing.md §2](../09-e2e-testing.md), а HS256 test-mode — это tech-debt-запись
  [TD-007](../100-known-tech-debt.md), не ADR (расширять нечего). Отдельный **ADR-061** делает решение
  и его риск явно обнаружимыми и перекрёстно связанными; тела существующих ADR не переписываются
  (immutability).
