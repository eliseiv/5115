# ADR-060 — Deploy-топология novirell: общий edge-nginx + разделяемая docker-сеть (изоляция двух стеков на одном хосте)

- **Статус:** Accepted (2026-07-10)
- **Расширяет / соседствует с:** [ADR-017](ADR-017-shared-server-traefik-deploy.md) (общий сервер за внешним Traefik). ADR-017 остаётся нормативным для **legacy**-инстансов (сервер `87.239.135.154`, edge = Traefik, сеть `web`, один `-f`, декларативные docker-labels). Данный ADR фиксирует **второй, отдельный** тип инстанса (сервер `49.12.189.77`, edge = чужой nginx, сеть `mas-net`, два `-f`, императивный `docker network connect`). Оба типа существуют параллельно.
- **Не отменяет:** [ADR-001](ADR-001-stack-choice.md) (стек), [ADR-010](ADR-010-backend-hosted-preview.md) (контракт reverse-proxy на `/v1/preview/*`), [ADR-033](ADR-033-llm-provider-abstraction.md)/[ADR-059](ADR-059-openai-default-provider.md) (провайдер по env; novirell — OpenAI-инстанс).
- **Порождает tech-debt:** [TD-037](../100-known-tech-debt.md) (доверие всей `/16` в `TRUSTED_PROXY_IPS`), [TD-038](../100-known-tech-debt.md) (императивное подключение к сети вне compose).
- **Операционная деталь (источник истины):** [07-deployment.md §Сервер novirell](../07-deployment.md#сервер-novirell-4912189477--nginx-edge-вместо-traefik).

## Контекст

Требовалось развернуть ещё один инстанс того же кода (домен `novirell.shop`, OpenAI-провайдер) на сервере `49.12.189.77`, где **уже** работает сторонний стек **mail-aggregator** (`postapp.store`, контейнеры `mas-*`), ломать который нельзя. Отличия сервера от инфраструктуры ADR-017 (Traefik):

1. **Edge — чужой nginx, не Traefik.** Порты 80/443 держит контейнер `mas-nginx` (reverse-proxy mail-aggregator). Своего Traefik / `/opt/edge` на хосте нет. TLS терминируется в `mas-nginx` сертификатом **хостового certbot** (`authenticator=webroot`, `webroot_path=/var/www/certbot`), не через ACME Traefik.
2. **Внешняя docker-сеть — `mas-net` (172.18.0.0/16), НЕ `web`.** Она принадлежит соседнему стеку mail-aggregator и является **общей**: в ней сосед уже держит generic service-name-алиасы `postgres` (→ `mas-postgres`), `redis` (→ `mas-redis`), `api` (→ `mas-api`).
3. **Compose всегда добавляет имя сервиса как сетевой алиас** в каждую сеть, к которой сервис подключён; явные `networks.<net>.aliases` этот алиас **не подавляют** (проверено на сервере, Compose 5.2.0).
4. **`mas-nginx` рендерит vhost-конфиги из `/etc/nginx/templates/*.template` через `envsubst` при каждом старте контейнера;** `/etc/nginx/conf.d/` — слой образа (не bind-mount).
5. **Хост ограничен по RAM** (3.8 GB, делится с mail-aggregator).

При первом реальном запуске (наш `api`, подключённый к `mas-net` средствами compose, адресующий БД по generic-именам) проявились **две коллизии неймспейса DNS в общей сети** (проверено вживую):

- **COLLISION 1 — наш `api` резолвил ЧУЖИЕ `postgres`/`redis`.** `api` мультихоумед (`default` + `mas-net`); неуточнённое имя `postgres`/`redis` docker-DNS отдавал из `mas-net` → приложение шло в чужую БД/кэш (в логах чужого Postgres `FATAL: password authentication failed for user "novirell"`; `/ready` показывал `redis: ok` — пинг уходил в чужой Redis).
- **COLLISION 2 — наш `api` перехватывал имя `api` в `mas-net`.** Compose добавлял `api` как алиас нашего контейнера на `mas-net`, где `api` уже принадлежит `mas-api`; vhost соседа проксирует `postapp.store` на `http://api:8080` → имя `api` резолвилось в два контейнера, трафик чужого продакшена мог попасть к нам.

Фактического ущерба не было (обнаружено и устранено при вводе). Решение фиксирует топологию и устранение коллизий, чтобы они не повторились и чтобы ADR-017 (Traefik-only) не читался как единственная нормативная deploy-схема.

## Решение

**Deploy-топология novirell = общий чужой edge-nginx + разделяемая внешняя docker-сеть `mas-net`, с project-unique адресацией БД и императивным подключением `api` к сети.**

1. **Edge — чужой `mas-nginx`, маршрут задаёт nginx-vhost, а не docker-labels.** Traefik-labels в `docker-compose.prod.yml` на этом сервере никто не читает (Traefik нет) — остаются инертными. Маршрут задаёт канонический vhost [`deploy/nginx/novirell.shop.conf`](../../deploy/nginx/novirell.shop.conf): HTTP→HTTPS (301) + ACME-challenge `location /.well-known/acme-challenge/ { root /var/www/certbot; }` (не редиректится), TLS от хостового certbot, upstream на сетевой алиас `novirell-api:8000` через docker-DNS (`resolver 127.0.0.11`, upstream в `$variable` для рантайм-резолва).

2. **vhost доставляется ДВУМЯ шагами (оба durable-критичны).** Т.к. `conf.d` — слой образа, а `mas-nginx` ре-рендерит `templates/*.template` → `conf.d` через `envsubst` при каждом старте: (a) `docker cp` vhost в `conf.d` + `nginx -t && nginx -s reload` — активно немедленно, но живёт лишь до пересоздания контейнера; (b) тот же контент как `.template` в bind-mount `/opt/mail-agregator/deploy/nginx/templates/` — durable-путь, переживает recreate. Пропуск шага (b) → после recreate `mas-nginx` домен отдаёт `502`.

3. **HSTS/security-заголовки.** TLS владеет nginx → nginx — единственный авторитетный источник **HSTS** (`add_header … always`, ровно один заголовок на всех путях, включая nginx-ошибки; upstream-HSTS приложения скрывается `proxy_hide_header`). CSP/`X-Frame-Options`/`X-Content-Type-Options` nginx **не** добавляет — их ставит приложение; `/v1/preview/*` — отдельный pass-through `location` (только HSTS), чтобы будущий глобальный CSP/X-Frame не перетёр sandbox-политику ([ADR-010](ADR-010-backend-hosted-preview.md)).

4. **COLLISION 1 устранена project-unique алиасами БД.** `postgres`/`redis` несут на сети `default` дополнительный алиас `${COMPOSE_PROJECT_NAME:-claude-ios}-postgres`/`-redis` (= `novirell-postgres`/`novirell-redis`), а `DATABASE_URL`/`REDIS_URL` в [`deploy/novirell.env.example`](../../deploy/novirell.env.example) адресуют именно их. Эти имена существуют только в нашей `default` → резолвинг однозначен. Compose сохраняет и generic `postgres`/`redis` как алиасы (legacy-инстансы с `.env`-хостами `postgres`/`redis` на сети `web` работают без изменений — behavioral no-op).

5. **COLLISION 2 устранена отвязкой `api` от edge-сети в compose + императивным подключением к `mas-net`.** Override [`docker-compose.novirell.yml`](../../docker-compose.novirell.yml) (применяется **только** на novirell вторым `-f`) отвязывает `api` от edge-сети: `networks: !override [default]` + `web: !reset null` — compose подключает `api` только к внутренней `default`, generic-алиас `api` в `mas-net` не попадает. Подключение к `mas-net` — **императивное** в деплой-workflow, после `up`, идемпотентно: `docker network connect --alias novirell-api mas-net novirell-api-1`. `docker network connect` **не** добавляет имя сервиса — только имя контейнера (`novirell-api-1`) и наш явный `--alias novirell-api`; generic `api` не появляется. vhost проксирует на `novirell-api`. Workflow **гвардит**: если в алиасах контейнера на `mas-net` встретится `api` — инстанс падает.

6. **Деплой требует ДВУХ `-f`.** Все compose-команды на novirell — `-f docker-compose.prod.yml -f docker-compose.novirell.yml`. Отдельный workflow [`.github/workflows/deploy-novirell.yml`](../../.github/workflows/deploy-novirell.yml) (gate на зелёный CI через `workflow_run` + ручной `workflow_dispatch`; один инстанс `novirell:novirell`; секреты `SSH_HOST`/`SSH_USER`/`SSH_KEY`): build → migrate → `up -d --no-build` → **императивный connect** → readiness-gate `novirell-api-1` → NON-FATAL smoke `https://novirell.shop/healthz`.

7. **Провайдер — OpenAI** (`LLM_PROVIDER=openai` + `OPENAI_API_KEY`, [ADR-033](ADR-033-llm-provider-abstraction.md)/[ADR-059](ADR-059-openai-default-provider.md)); задаётся через env инстанса, не сменой кодового дефолта.

8. **RAM-митигация.** `GUNICORN_WORKERS=2` (env, Dockerfile CMD `sh -c … -w ${GUNICORN_WORKERS:-4}`, дефолт `4` — обратная совместимость legacy) + **2 GB swap** (`/swapfile`, в `/etc/fstab`). DB-пул: `(10+5)*2 = 30 < max_connections(100)`.

9. **`TRUSTED_PROXY_IPS=172.18.0.0/16`** (вся подсеть `mas-net`) — необходимо, т.к. `mas-nginx` проставляет `X-Forwarded-For` и его IP не закреплён. Публичный XFF-спуфинг закрыт overwrite `X-Forwarded-For $remote_addr` на edge; остаточный in-network-вектор — [TD-037](../100-known-tech-debt.md).

## Рассмотренные альтернативы

- **Переименование сервиса `api`** (чтобы не коллизировать с `mas-api`). **Отвергнуто:** ломает детерминированное имя контейнера `<proj>-api-1` (nginx-upstream + readiness-gate `${proj}-api-1`) на **всех** инстансах, включая работающие legacy; при этом переименованный сервис **всё равно** утёк бы своим именем-алиасом в `mas-net`. Более инвазивно и не решает корень (COLLISION 2 — про сам факт service-name-алиаса).
- **Адресация upstream по имени контейнера (`novirell-api-1`) вместо алиаса.** Работает (`docker network connect` добавляет и имя контейнера), но имя жёстко связано со схемой compose `<project>-<service>-<index>` и `--scale`. Явный `--alias novirell-api` — handle под нашим контролем, стабильный при масштабировании; выбран для vhost-upstream. Имя контейнера остаётся для readiness-gate/аргумента `connect`.
- **Отдельная сеть только с nginx (`nginx`↔`api`) вместо общей `mas-net`.** Устранила бы обе коллизии декларативно и закрыла бы TD-037/TD-038. **Отвергнуто на MVP:** требует подключить **чужой** `mas-nginx` к нашей сети → правка стека mail-aggregator (вне нашего контроля, риск сломать соседа). Зафиксировано как путь закрытия TD-037/TD-038.
- **Публикация порта `api` на loopback хоста (`127.0.0.1:PORT`) + `proxy_pass` nginx на host-gateway.** Убрала бы нашу зависимость от `mas-net` целиком. **Отвергнуто:** `mas-nginx` — контейнер, доступ к host-loopback требует `host.docker.internal`/host-gateway-хаков в чужом контейнере (мы им не управляем) либо `network_mode: host`; публикация порта расширяет поверхность и конфликтует с принципом «`api` без публикации портов» ([ADR-017](ADR-017-shared-server-traefik-deploy.md)). Сетевой алиас в общей `mas-net` проще и не требует правок соседа.

## Последствия

- **Плюсы:** второй инстанс изолирован от mail-aggregator по данным (project-unique БД-алиасы, отдельные volumes/секреты) и по трафику (нет generic-алиаса `api` в `mas-net`); переиспользован общий `docker-compose.prod.yml` без форка (override + env); TLS/renewal — на хостовом certbot соседа, свою ACME-инфраструктуру не заводим.
- **Минусы / принятый долг:**
  - **[TD-037](../100-known-tech-debt.md)** — `TRUSTED_PROXY_IPS` доверяет всей `/16` (не `/32` nginx): in-mas-net-контейнер может спуфить `client_ip` в обход nginx. `/32`-пин отвергнут (IP nginx не закреплён → тихая деградация rate-limit).
  - **[TD-038](../100-known-tech-debt.md)** — подключение `api` к `mas-net` императивно вне compose, обязано выполняться **каждый** деплой; пропуск → `502` (fail-safe: потеря доступности novirell, не перехват чужого трафика). Атрибут «на `mas-net` без алиаса `api`» не декларативен в compose.
  - Доставка vhost durable только через `.template` в **чужом** каталоге mail-aggregator (не в нашем репо) — двухшаговая операция, требует runbook.
- **Проверка вживую после фикса:** в `mas-net` наш контейнер имеет `Aliases: [novirell-api]` (НИКОГДА `api`); `api` резолвится только в `mas-api` (172.18.0.6). Smoke: `/healthz`→200 `{"status":"ok"}`, `/ready`→200 `{"db":"ok","redis":"ok"}` (наши БД/кэш), `/metrics`→404 на edge, ровно один HSTS, `http://`→301, `/docs`→404, ACME-путь не редиректится.
- **Триггер пересмотра:** переход mail-aggregator на выделенную `nginx↔api`-сеть или закрепление статического IP `mas-nginx` (закрывает TD-037/TD-038); появление декларативного подавления service-name-алиаса в будущих Compose; вынос сервиса на собственный edge — новый/расширенный ADR.
