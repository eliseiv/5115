# Deploy / rollback runbook (infra)

Implements the contract in `docs/07-deployment.md`. This file is an infra artifact;
the authoritative deployment spec lives in `docs/07-deployment.md` (owned by architect).

## Deploy target — shared server + EXTERNAL Traefik + GitHub Actions SSH (ADR-017)
The deploy target is fixed by the infrastructure owner (`docs/07-deployment.md` §Топология MVP,
`docs/adr/ADR-017-shared-server-traefik-deploy.md`, revises `docs/100-known-tech-debt.md#TD-005`):

- **Shared Linux server** (Ubuntu 22.04, `87.239.135.154`, root), stack dir `/opt/claude-ios`.
- **External edge-proxy Traefik** in `/opt/edge` owns ports 80/443, terminates TLS, issues
  Let's Encrypt certs (ACME) and routes by Host. **We do NOT run a reverse proxy / TLS / nginx /
  Caddy.** Our `api` is reached only through Traefik over the **external** docker network `web`
  (`docker network create web` — already created on the server).
- **Image is BUILT ON THE SERVER** (`docker compose up -d --build`), not pulled from a registry —
  there is no immutable registry tag in this scheme.

The active prod artifacts are:
- `docker-compose.prod.yml` — prod stack: `api` (`expose: 8000`, no published ports, on `web`
  external + `default`, Traefik docker-labels) + `postgres` 16 + `redis` 7 (both `default` only,
  no ports) + one-shot `migrate`. No proxy/Caddy service.
- `.env.prod.example` — prod env template (placeholders only; the real `.env` lives on the server
  and is gitignored). Includes `SERVICE_DOMAIN=broadnova.shop`, `TRAEFIK_CERTRESOLVER=le` (both
  PUBLIC config, not secrets), and `TRUSTED_PROXY_IPS` (filled on the server).
- `.github/workflows/deploy.yml` — GitHub Actions SSH deploy (push to `main` / manual).
- `docker-compose.prod.observability.yml` + `infra/observability/prometheus.prod.yml` — optional
  Prometheus overlay (loopback only, internal `default` network, never on `web`).

### Legacy artifacts (NOT used under ADR-017)
Moved to `infra/legacy/` and marked DEPRECATED — kept for reference only, never deployed:
- `infra/legacy/Caddyfile`, `infra/legacy/nginx.conf.example` — our own reverse-proxy/TLS is no
  longer used (TLS/ACME is the external Traefik's job).
- `infra/legacy/deploy-vps.sh` — dedicated-VPS + registry/immutable-tag + in-stack Caddy SSH
  script, superseded by `.github/workflows/deploy.yml`.

`infra/deploy/deploy.sh` is the platform-neutral generic seam (registry/immutable-tag oriented).
It is **not** the active prod path under ADR-017 (image is built on the server, no registry).
Kept only as a generic reference for a future registry-based target (would need a new ADR).

## Release contract (ADR-017)
- The image is built on the server from sources in `/opt/claude-ios` (no registry / immutable tag).
- Pre-deploy: `docker compose run --rm migrate` (`alembic upgrade head`, chain 0001->0004;
  expand/contract, backward-compatible) — runs to completion before the new `api` starts.
- Rebuild + recreate `api`: `docker compose up -d --build`. Single host, 1 container, Gunicorn
  `-w 4`; expand/contract keeps the old image compatible during the swap. The external Traefik
  picks up the new container by labels/network `web`.
- Health gate: `GET /healthz` (liveness alias of `/health`), then `GET /ready` (DB + Redis).
- **Rollback** (no immutable tag): `git checkout <prev-commit>` in `/opt/claude-ios` +
  `docker compose -f docker-compose.prod.yml up -d --build`. Schema is NOT reverted
  (expand/contract keeps the old code compatible).

## CI/CD
- `.github/workflows/ci.yml` — gate: ruff format/lint, mypy, pytest+coverage, docker build
  (validation only, **no registry push** under ADR-017). Blocks merge on failure.
- `.github/workflows/deploy.yml` — deploy: SSH to the server -> `git pull` -> migrate -> build+up
  -> public `/healthz` smoke. Triggered on push to `main` or manual `workflow_dispatch`.

GitHub Secrets (Settings -> Secrets and variables -> Actions) — NEVER hardcoded:
- `SSH_HOST=87.239.135.154`
- `SSH_USER=root`
- `SSH_PRIVATE_KEY` (private key; its public half in the server's `~/.ssh/authorized_keys`)

Optional repo **Variable** (not a secret): `SERVICE_DOMAIN=broadnova.shop` — enables the public
`/healthz` smoke check in `deploy.yml` (skipped if unset, e.g. before the domain/A-record exists;
non-fatal on the first deploy while DNS/cert settle — see the smoke step note in `deploy.yml`).

## Launch checklist (broadnova.shop) — do this before the first deploy
1. **DNS A-record:** `broadnova.shop` -> `87.239.135.154` (required for Traefik's Let's Encrypt
   ACME challenge; must exist BEFORE launch — Q-017-1 resolved).
2. **GitHub Secrets** (Settings -> Secrets and variables -> Actions): `SSH_HOST=87.239.135.154`,
   `SSH_USER=root`, `SSH_PRIVATE_KEY` (public half in the server's `~/.ssh/authorized_keys`).
3. **GitHub repo Variable** (not a secret): `SERVICE_DOMAIN=broadnova.shop` (enables the public
   smoke check in `deploy.yml`).
4. **Server prerequisites** (server owner): external network `docker network create web` (already
   created on 87.239.135.154); stack dir `/opt/claude-ios` (`git clone`); `cp .env.prod.example .env`.
5. **Fill `/opt/claude-ios/.env`** from the secret manager:
   - `SERVICE_DOMAIN=broadnova.shop` (already in the template).
   - `TRAEFIK_CERTRESOLVER=le` (already in the template; ACME resolver name in the shared Traefik
     `/opt/edge`, default on the `websecure` entrypoint — Q-017-2 resolved).
   - `TRUSTED_PROXY_IPS` = the `web` network subnet (`docker network inspect web` ->
     `.[0].IPAM.Config[].Subnet`, typically `172.x.0.0/16`).
   - all real secrets (Anthropic / JWT / KMS / DB / Redis / admin / preview / metrics token).
6. **First bring-up** (on the server): `migrate` then `up -d --build` (see below).
7. **Verify:** `curl -fsS https://broadnova.shop/healthz` returns `200` once Traefik routes and the
   `le` resolver has issued the TLS cert.

## First deploy / manual usage (run ON the server, in /opt/claude-ios)
```bash
# 0) one-time prerequisites (server owner): external network + stack dir + .env
docker network create web            # already created on 87.239.135.154
git clone <repo> /opt/claude-ios
cp .env.prod.example .env            # then fill .env from the secret manager (see below)

# 1) migrate then bring up the stack (image built on the server)
docker compose -f docker-compose.prod.yml --env-file .env run --rm migrate
docker compose -f docker-compose.prod.yml --env-file .env up -d --build

# 2) smoke
curl -fsS https://${SERVICE_DOMAIN}/healthz     # 200 once Traefik routes + TLS is issued
```

### Required `.env` values to launch (filled on the server, not committed)
- `SERVICE_DOMAIN=broadnova.shop` (Q-017-1 resolved). Its **A-record MUST point to 87.239.135.154
  before launch** (Traefik ACME challenge). Already set in `.env.prod.example`.
- `TRAEFIK_CERTRESOLVER=le` (Q-017-2 resolved) — the ACME resolver name configured in the shared
  Traefik (`/opt/edge`), default on the `websecure` entrypoint. Already set in `.env.prod.example`.
- `TRUSTED_PROXY_IPS` — the `web` network subnet so per-IP rate limiting sees the real client IP
  from Traefik's `X-Forwarded-For`. Find it on the server:
  `docker network inspect web` -> `.[0].IPAM.Config[].Subnet` (typically `172.x.0.0/16`).
- All secrets (see below).

## Rollback (no registry/immutable tag — ADR-017)
```bash
cd /opt/claude-ios
git log --oneline -n 5                # find the previous good commit
git checkout <prev-commit>
docker compose -f docker-compose.prod.yml --env-file .env run --rm migrate   # if needed
docker compose -f docker-compose.prod.yml --env-file .env up -d --build
curl -fsS https://${SERVICE_DOMAIN}/healthz
# return to the branch tip once a fix is ready: git checkout main && git pull
```

## Secrets
All secrets (`ANTHROPIC_API_KEY`, JWT keys, `KMS_LOCAL_MASTER_KEY`/`KMS_*`, `APPSTORE_*`,
DB creds (`DATABASE_URL`/`POSTGRES_PASSWORD`), `REDIS_URL`, `METRICS_SCRAPE_TOKEN`,
`ADMIN_API_SECRET` (+ `ADMIN_API_SECRET_PREV` during rotation), `PREVIEW_URL_SECRET`) come from
the server's secret manager — never from a committed file or baked into the image
(05-security.md). In prod they live in `.env` in `/opt/claude-ios` (gitignored), loaded by the
api/migrate containers via `env_file` in `docker-compose.prod.yml`. The isolated secrets (admin
token + preview HMAC + KMS master key) are mutually independent and independent of the
JWT/Anthropic secrets (ADR-009, ADR-010, ADR-003): each is provisioned and rotated separately.
`redaction` (05-security.md) keeps `X-Admin-Token`, `*secret*`, `*token*`, `*key*` out of logs.

**Not a secret:** `TOKEN_PRODUCTS` (consumable productId->credits mapping, ADR-015) and
`SERVICE_DOMAIN`/`TRAEFIK_CERTRESOLVER` are config, not credentials. `TOKEN_PRODUCTS` must MATCH
the IAP products configured in App Store Connect (prod-checklist).

`.env.example` and `.env.prod.example` are placeholder templates only — never real values.

## Pre-commit security checklist (MANDATORY before adding/committing)
```bash
# Each path MUST be reported as ignored (non-empty output + exit 0).
git check-ignore -v .env .env.prod .env.e2e \
  .secrets/e2e/jwt_private_key.pem \
  .secrets/e2e/generated_secrets.txt
```
- All listed paths must be reported as ignored. If any is missing, STOP — do not commit.
- The committed `.env.prod.example` MUST contain only placeholders (`<...>`), never real
  secrets. Sanity-check before commit (each line below should print NOTHING):
  ```bash
  grep -nE 'sk-ant-[A-Za-z0-9]' .env.prod.example      # real Anthropic key
  grep -nE '(ADMIN_API_SECRET|PREVIEW_URL_SECRET|KMS_LOCAL_MASTER_KEY)=[A-Za-z0-9+/]{20,}' .env.prod.example
  grep -nE '(POSTGRES_PASSWORD|METRICS_SCRAPE_TOKEN)=[A-Za-z0-9+/]{16,}' .env.prod.example
  ```
- NEVER use `git add -f` (force) on `.env*` (except `.env*.example`), `.secrets/`, `*.pem`, `*.key`.
- Defense-in-depth: `.gitignore` ignores `.env`, `.env.prod`, `.secrets/`, `*.pem`, `*.key`;
  `.dockerignore` keeps `.env*` (except `.env.example`), `.secrets/`, `*.pem`, `*.key`, and
  `infra/` out of the Docker build context (no proxy config / deploy scripts / secrets baked in).
