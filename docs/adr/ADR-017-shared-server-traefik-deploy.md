# ADR-017 — Deploy-топология: общий сервер за внешним Traefik + GitHub Actions SSH

- **Статус:** Accepted (2026-06-02; расширен 2026-06-10 разделом «Мульти-инстанс / клонирование сервиса»)
- **Контекст ревизует:** [TD-005](../100-known-tech-debt.md) (зафиксирован VPS + Caddy-standalone). Не отменяет [ADR-001](ADR-001-stack-choice.md) (стек) и [ADR-010](ADR-010-backend-hosted-preview.md) (контракт reverse-proxy на `/v1/preview/*`).
- **Расширение (2026-06-10):** добавлен паттерн мульти-инстанс / клонирования за общим Traefik (`COMPOSE_PROJECT_NAME`-параметризация, изоляция доменов/данных/секретов, per-instance JWT keypair). Playbook — [07-deployment.md §Мульти-инстанс](../07-deployment.md#мульти-инстанс--клонирование-сервиса). Связано с [Q-017-3](../99-open-questions.md).

## Контекст

Ранее ([TD-005](../100-known-tech-debt.md), 2026-06-02) deploy-target был зафиксирован как **отдельный VPS** с **собственным** reverse-proxy (Caddy/nginx) внутри нашего `docker compose`-стека: Caddy держал порты 80/443, терминировал TLS (auto-ACME), проксировал на `api` по `127.0.0.1:8000`. Выкатка — `deploy-vps.sh` по SSH.

Владелец инфраструктуры изменил требования: сервис размещается **не на выделенном VPS, а на общем Linux-сервере** (Ubuntu 22.04, `87.239.135.154`), где уже работают другие сервисы (`music-backend`) и **общий edge-прокси Traefik** в `/opt/edge`. Traefik держит порты 80/443, терминирует TLS, авто-выпускает Let's Encrypt-сертификаты и роутит по доменам для всех сервисов сервера.

Жёсткие требования владельца к встраиванию:
1. НЕ публиковать порты 80/443 (конфликт с Traefik) — только `expose` внутреннего `8000`.
2. Контейнер `api` — в **внешней** docker-сети `web` (общая с Traefik, создана `docker network create web`) + `default` (внутренняя для PG/Redis).
3. Маршрутизация — через **docker-labels** (Traefik service discovery), не через наш конфиг прокси.
4. SSL/nginx/Caddy у нас **не настраивается** — TLS целиком ответственность Traefik. `postgres`/`redis` — только в `default`, без публикации портов.
5. CI/CD — GitHub Actions → SSH на сервер → сборка/выкатка на сервере (build из исходников, не из registry). Принцип «build на сервере» неизменен; **детальный per-instance процесс** (`git pull --ff-only` → explicit `build` → `migrate` → `up -d --no-build` → readiness-gate; `set -uo pipefail` без `-e`, `script_stop: false`, `$FAILED`-аккумуляция, финальный `exit 1`) зафиксирован в [07-deployment.md §Процедура деплоя](../07-deployment.md#процедура-деплоя-github-actions--ssh) и [§CI/CD INSTANCES-loop](../07-deployment.md#cicd-контракт-instances-loop-мульти-инстанс) — источник истины операционной детали.

## Решение

**Deploy-топология MVP = общий сервер + внешний Traefik + GitHub Actions SSH-деплой.**

1. **Reverse-proxy и TLS — внешний Traefik, не наша ответственность.** Наш стек **не содержит** reverse-proxy-контейнера, не держит порты 80/443, не терминирует TLS, не управляет сертификатами. Прежние Caddy-артефакты (`infra/legacy/Caddyfile`, `infra/legacy/nginx.conf.example`, наш TLS) в этой схеме **не используются** (перенесены в `infra/legacy/` с DEPRECATED-баннером — см. [07-deployment.md](../07-deployment.md)).

2. **Сеть:** `api` подключён к двум сетям:
   - `web` — `external: true` (общая с Traefik), через неё Traefik проксирует входящий трафик на `api:8000`;
   - `default` — внутренняя сеть стека для связи `api` ↔ `postgres`/`redis`.
   `postgres`/`redis` — **только** в `default`, без публикации портов наружу.

3. **`api`** — `expose: 8000` (uvicorn/gunicorn), **без** `ports:` для 80/443/публичного маппинга. Доступ снаружи — исключительно через Traefik по `web`.

4. **Маршрутизация — docker-labels** на сервисе `api` (Traefik подхватывает):
   ```
   traefik.enable=true
   traefik.docker.network=web
   traefik.http.routers.<service>.rule=Host(`${SERVICE_DOMAIN}`)
   traefik.http.routers.<service>.entrypoints=websecure
   traefik.http.routers.<service>.tls.certresolver=${TRAEFIK_CERTRESOLVER}
   traefik.http.services.<service>.loadbalancer.server.port=8000
   ```
   `certresolver` параметризован через `${TRAEFIK_CERTRESOLVER}` = **`le`** — имя ACME-резолвера общего Traefik (из `/opt/edge`; [Q-017-2](../99-open-questions.md), Closed 2026-06-02). `le` сделан **дефолтным** на entrypoint `websecure` (`--entrypoints.websecure.http.tls.certresolver=le`), поэтому label `tls.certresolver` **опционален** (сертификат выпускается автоматически для любого HTTPS-роутера); явный label рекомендован для надёжности. Домен — `${SERVICE_DOMAIN}` = **`broadnova.shop`** ([Q-017-1](../99-open-questions.md), Closed 2026-06-02).

5. **CI/CD = GitHub Actions SSH workflow.** Сборка/выкатка на сервере: SSH (`appleboy/ssh-action`, `script_stop: false`) → per-instance loop по `$INSTANCES` (`cd /opt/<dir>` → `git pull --ff-only` → explicit `docker compose build api migrate` → `run --rm migrate` → `up -d --no-build` → readiness-gate `${proj}-api-1` healthy → NON-FATAL smoke `/healthz`). Образ собирается **на сервере** (build из исходников в `/opt/<dir>`), а не пушится из registry. Remote-скрипт под `set -uo pipefail` **без `-e`** (loop обязан пройти все инстансы); реальные сбои копятся в `$FAILED`, финальный `exit 1` краснит job. `up -d --build` **заменён** на explicit build → migrate → `up --no-build` + readiness-gate, т.к. совмещённая команда отдавала транзиентный non-zero, обрывавший loop под прежним `set -e`. Детальный листинг и обоснование — [07-deployment.md §Процедура деплоя](../07-deployment.md#процедура-деплоя-github-actions--ssh) и [§CI/CD INSTANCES-loop](../07-deployment.md#cicd-контракт-instances-loop-мульти-инстанс). GitHub Secrets: `SSH_HOST=87.239.135.154`, `SSH_USER=root`, `SSH_PRIVATE_KEY`. `apply_release`-seam ([ADR-001]/инфра) специализируется под этот SSH-flow.

6. **`/healthz`** — публичный endpoint `200` как алиас `GET /health` (для healthcheck Traefik и smoke). Контракт зафиксирован в [API-REFERENCE.md](../API-REFERENCE.md) и [api-gateway/02-api-contracts.md](../modules/api-gateway/02-api-contracts.md). Без auth.

7. **`TRUSTED_PROXY_IPS`** в prod **обязан** включать адрес/подсеть Traefik (docker-сеть `web`). Traefik проставляет `X-Forwarded-For`; без доверия к нему `client_ip` берётся как IP Traefik → per-IP rate limit неработоспособен. См. [05-security.md](../05-security.md#доверенный-reverse-proxy-и-определение-client-ip-anti-spoofing).

8. **DNS:** A-запись домена сервиса `broadnova.shop` → `87.239.135.154` должна существовать **до** запуска (Traefik ACME-challenge выпускает сертификат по домену). Предзапусковый операционный пункт — см. [07-deployment.md prod-checklist](../07-deployment.md#prod-readiness-checklist-must-configure-before-launch).

9. **Инвариант сервера:** `DOCKER_MIN_API_VERSION=1.24` уже задан на сервере — не трогать.

## Последствия

**Плюсы:**
- Меньше нашей ответственности: TLS, ACME, edge-роутинг — на общем Traefik. Наш стек проще (нет proxy-контейнера, Caddyfile, certbot).
- Совместное использование сервера с другими сервисами без конфликта портов (изоляция через сети + labels).
- Деплой операционно прост: `git pull --ff-only` + явные `docker compose build` / `run --rm migrate` / `up -d --no-build` на сервере по SSH из GitHub Actions (образ собирается на сервере, без registry; см. п.5 выше и [07-deployment.md §Процедура деплоя](../07-deployment.md#процедура-деплоя-github-actions--ssh)).

**Минусы / риски:**
- Сборка образа **на сервере** при каждом деплое (нет immutable registry-tag) → дольше выкатка, нагрузка на сервер при build, rollback — через `git checkout <prev-commit>` + rebuild, а не переключение тега. Зафиксировано в [07-deployment.md §Откат](../07-deployment.md#откат).
- Контракт pass-through заголовков на `/v1/preview/*` ([ADR-010](ADR-010-backend-hosted-preview.md)) теперь — ответственность **внешнего** Traefik (вне нашего репозитория). Требование к Traefik: не перетирать/не дублировать sandbox-заголовки приложения и не инжектить cookies на `/v1/preview/*`. Зафиксировано как операционное требование к владельцу Traefik в [07-deployment.md](../07-deployment.md).
- Зависимость от чужого Traefik: его недоступность/мисконфиг роняет роутинг. Контроль конфигурации Traefik вне нашей зоны.
- `root`-доступ по SSH из CI — повышенный риск компрометации `SSH_PRIVATE_KEY`; ключ — только в GitHub Secrets, ротация при подозрении.

**Закрытое тех-долговое последствие:** [TD-005](../100-known-tech-debt.md) обновлён — финальная схема = shared-server + Traefik + GitHub Actions SSH (не противоречит, уточняет deploy-target).

## Расширение: мульти-инстанс / клонирование сервиса (2026-06-10)

**Контекст расширения.** Тот же общий сервер `87.239.135.154` и тот же общий edge-Traefik (`/opt/edge`) должны обслуживать **несколько независимых инстансов** одного и того же кода claude-ios под **разными доменами** (первый — `broadnova.shop`, второй — `avelyraweb.shop`, DNS уже → `87.239.135.154`). Каждый инстанс — изолированный стек (свой `api`+`postgres`+`redis`, свои тома, свои секреты), маршрутизируемый своим `Host()`-правилом через общий Traefik. Паттерн портирован из соседнего сервиса lovable-ai и адаптирован под **простую** архитектуру claude-ios (нет build-фермы, egress-proxy, worker/beat, S3, host-dir провижининга — только `api`+`postgres`+`redis`+`migrate`+per-instance `.secrets/` JWT keypair).

**Решение (расширяет п.1–9 выше, не отменяет их):**

10. **Инстанс-префикс — единый ключ `COMPOSE_PROJECT_NAME`.** Имя docker-compose project задаётся флагом `-p <inst>` при деплое (приоритет Compose: CLI `-p` > `COMPOSE_PROJECT_NAME` env > top-level `name:` в файле > basename каталога). Это автоматически изолирует **сети, тома, контейнеры** инстанса под префиксом project-name.

11. **Параметризация `docker-compose.prod.yml` (минимальная, через `${COMPOSE_PROJECT_NAME:-claude-ios}`):**
    - **image-теги:** `image: ${COMPOSE_PROJECT_NAME:-claude-ios}-backend:prod` (сервисы `migrate`, `api`) — у каждого инстанса свой локально собранный образ, без коллизии тегов.
    - **Traefik router/service-имена:** `traefik.http.routers.${COMPOSE_PROJECT_NAME:-claude-ios}.*` и `traefik.http.services.${COMPOSE_PROJECT_NAME:-claude-ios}.*` — уникальные имена роутера/сервиса в общем Traefik (иначе два инстанса с router-именем `claude-ios` затрут друг друга).
    - **Host-правило:** `Host(\`${SERVICE_DOMAIN}\`)` — **уже** параметризовано (п.4), переопределяется через `.env` каждого инстанса.
    - **cert-resolver:** `${TRAEFIK_CERTRESOLVER}` — **уже** параметризован (п.4), общий `le` для всех инстансов.

12. **Инвариант обратной совместимости (КРИТИЧНО).** Любая новая параметризация **обязана** иметь дефолт = текущему захардкоженному значению (`claude-ios`). Формальный критерий: для существующего `/opt/claude-ios/.env` (без ключа `COMPOSE_PROJECT_NAME`) команда `docker compose -f docker-compose.prod.yml --env-file .env config` даёт **идентичный** результат до и после параметризации — те же project-name (`claude-ios`), image (`claude-ios-backend:prod`), router/service-имена (`claude-ios`), сети, тома. Иначе — регрессия живого прода broadnova.shop. Существующий деплой `docker compose -f docker-compose.prod.yml --env-file .env up -d` (БЕЗ `-p`) и новый `docker compose -p claude-ios -f ... up -d` должны быть эквивалентны (project-name `claude-ios` совпадает с basename `/opt/claude-ios`).

13. **Изоляция и разделяемое.** Разделяется **только** внешняя сеть `web` + сам edge-Traefik (общие для всех сервисов сервера). Изоляция инстансов: разные `Host()` (по `SERVICE_DOMAIN`), уникальные router/service-имена (префикс project-name), отдельные `default`-сети и тома (`pgdata`/`redisdata` именуются `<project>_pgdata`), свои `.env` и `.secrets/`. Инстансы **не делят** БД, Redis, секреты, JWT keypair.

14. **Per-instance секреты и JWT keypair.** Клон генерирует **свежие** секреты (не копии соседа): `POSTGRES_PASSWORD`, `ANTHROPIC_API_KEY`, `ADMIN_API_SECRET`, `KMS_LOCAL_MASTER_KEY`, `PREVIEW_URL_SECRET`, `METRICS_SCRAPE_TOKEN`, `STOREKIT_TEST_SECRET` (если test-mode) и **собственный RSA JWT keypair** в `/opt/<inst>/.secrets/` (`jwt_private.pem`+`jwt_public.pem`, `chown 10001:10001`, `chmod 640`, каталог `750`). `JWT_ISSUER=https://<домен>`, `SERVICE_DOMAIN=<домен>`, `TRUSTED_PROXY_IPS`=подсеть `web`. Это аналог host-dir провижининга lovable-ai, сведённый к одному `.secrets/`-каталогу.

15. **CI/CD — INSTANCES-loop.** Существующий single-instance deploy-job переходит на итерацию по нормативному списку `INSTANCES` (формат `dir:project`, claude-ios **первым** для backward-compat). Поскольку `-p claude-ios` совпадает с текущим неявным project-name, добавление цикла — no-op для живого инстанса. Спецификация — [07-deployment.md §CI/CD: INSTANCES-loop](../07-deployment.md#cicd-контракт-instances-loop-мульти-инстанс).

**Последствия расширения.** Плюсы: повторно используется один и тот же код/compose/CI для N доменов без форка; полная изоляция данных и секретов между инстансами; живой broadnova.shop не затрагивается (дефолты = текущие значения). Минусы/риски: сборка образа на сервере умножается на число инстансов (нагрузка при одновременном деплое); общий Traefik — единая точка отказа для всех доменов; операционная дисциплина обязательна (`-p` нельзя забыть — иначе клон затрёт тома/роутер claude-ios; свежие секреты нельзя копировать у соседа). Связано с [ADR-018](ADR-018-embedded-auth-issuer.md) (per-instance JWT keypair) и [Q-017-3](../99-open-questions.md) (статус выката avelyraweb.shop).

## Альтернативы

- **Выделенный VPS + собственный Caddy (прежний TD-005).** Отклонено: владелец предоставляет общий сервер с уже работающим Traefik; дублировать edge-прокси нельзя (конфликт 80/443).
- **Сборка образа в CI + push в registry, на сервере только pull.** Лучше для скорости/immutability, но требует registry-инфраструктуры и креденшелов; владелец зафиксировал `git pull && compose up --build` на сервере. Оставлено как возможное улучшение (новый ADR при вводе registry).
- **Свой Traefik/nginx внутри нашего стека за общим Traefik (двойной прокси).** Отклонено: избыточно, второй прокси не нужен — Traefik роутит напрямую на `api:8000` по labels.
