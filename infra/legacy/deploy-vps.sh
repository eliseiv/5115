#!/usr/bin/env bash
# =============================================================================
# DEPRECATED / LEGACY — NOT USED in the shared-server + external Traefik scheme (ADR-017).
# This script assumes a dedicated VPS, a registry + immutable image tag (IMAGE/IMAGE_TAG),
# and an in-stack Caddy. The current scheme builds the image ON THE SERVER and deploys via
# GitHub Actions SSH (.github/workflows/deploy.yml: git pull -> compose run migrate ->
# compose up -d --build). Rollback is now `git checkout <prev-commit>` + rebuild, NOT a tag
# switch. Kept for reference only — do NOT run. See docs/07-deployment.md, infra/deploy/README.md.
# =============================================================================
#
# VPS / SSH + Docker Compose specialisation of the deploy contract
# (docs/07-deployment.md §Процедура деплоя, TD-005 CLOSED 2026-06-02:
# deploy-target fixed to single VPS + Docker Compose by the user).
#
# This is the concrete `apply_release` realisation that the platform-neutral
# infra/deploy/deploy.sh leaves as a seam (APPLY_RELEASE_CMD / apply_release).
# Run it ON THE VPS, in the stack directory (where docker-compose.prod.yml and
# .env.prod live). It is idempotent: re-running with the same IMAGE_TAG re-converges
# (alembic upgrade head is a no-op once applied; `up -d` recreates only on change).
#
# Procedure (docs/07-deployment.md):
#   1. pull/build the new immutable image tag
#   2. migrate: docker compose run --rm migrate  (alembic upgrade head, 0001->0004)
#   3. recreate api with the new tag (expand/contract keeps old api compatible)
#   4. health-gate: GET /health then GET /ready (db=ok, redis=ok) via the proxy/host
#   5. on failure -> rollback to the PREVIOUS immutable tag and re-health-gate
set -Eeuo pipefail

# --- inputs (env / args; NO secrets in this file — secrets live in .env.prod) -----------
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env.prod}"
IMAGE="${IMAGE:?set IMAGE=ghcr.io/<org>/<repo> (must match .env.prod)}"
IMAGE_TAG="${IMAGE_TAG:?set IMAGE_TAG=<git-sha> (new immutable tag to deploy)}"
PREVIOUS_TAG="${PREVIOUS_TAG:-}"     # required for rollback; record it before each deploy
# Health-gate via loopback by default (api publishes 127.0.0.1:8000); override to go via the
# public domain through the proxy for an end-to-end check.
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
READY_URL="${READY_URL:-${HEALTH_URL%/health}/ready}"
SMOKE_RETRIES="${SMOKE_RETRIES:-30}"
SMOKE_INTERVAL="${SMOKE_INTERVAL:-5}"
# If true, pull the image from the registry; otherwise build locally on the VPS.
PULL_IMAGE="${PULL_IMAGE:-true}"

# docker compose with the prod file + env file pinned for every call.
dc() { docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" "$@"; }

log() { printf '[deploy-vps] %s\n' "$*" >&2; }

require_env_file() {
  [[ -f "${ENV_FILE}" ]] || { log "FATAL: ${ENV_FILE} not found (secrets from secret manager)"; exit 2; }
}

# Pin IMAGE_TAG for compose interpolation (docker-compose.prod.yml uses ${IMAGE}:${IMAGE_TAG}).
export_tag() { export IMAGE IMAGE_TAG; }

pull_or_build() {
  export_tag
  if [[ "${PULL_IMAGE}" == "true" ]]; then
    log "pulling ${IMAGE}:${IMAGE_TAG}"
    dc pull api migrate
  else
    log "building image locally for tag ${IMAGE}:${IMAGE_TAG}"
    dc build api
  fi
}

run_migrations() {
  # Pre-deploy, one-shot. DATABASE_URL is read from .env.prod inside the container.
  # Expand/contract (0001->0004) — backward-compatible, safe while old api still serves.
  export_tag
  log "running migrate job (alembic upgrade head, chain 0001->0004)"
  dc run --rm migrate
}

apply_release() {
  # SEAM specialised for VPS + docker compose (TD-005). Recreate api with the new tag.
  # `up -d` only recreates containers whose config/image changed -> converging/idempotent.
  export_tag
  log "applying release ${IMAGE}:${IMAGE_TAG} (docker compose up -d api)"
  dc up -d api
  # Ensure the proxy is up (no-op if already running with unchanged config).
  dc up -d caddy
}

smoke_test() {
  local url="$1" name="$2" i
  for ((i = 1; i <= SMOKE_RETRIES; i++)); do
    if curl -fsS --max-time 5 "${url}" >/dev/null 2>&1; then
      log "${name} OK (${url})"
      return 0
    fi
    log "${name} not ready yet (${i}/${SMOKE_RETRIES}) — sleeping ${SMOKE_INTERVAL}s"
    sleep "${SMOKE_INTERVAL}"
  done
  log "${name} FAILED after ${SMOKE_RETRIES} attempts (${url})"
  return 1
}

rollback() {
  if [[ -z "${PREVIOUS_TAG}" ]]; then
    log "ROLLBACK requested but PREVIOUS_TAG is empty — manual intervention required"
    return 1
  fi
  log "ROLLBACK: redeploying previous immutable tag ${IMAGE}:${PREVIOUS_TAG}"
  # Schema stays forward (expand/contract): old code is compatible with the new schema,
  # so we redeploy the previous image WITHOUT reverting migrations.
  IMAGE_TAG="${PREVIOUS_TAG}" apply_release
  smoke_test "${HEALTH_URL}" "health(rollback)"
}

main() {
  require_env_file
  case "${1:-deploy}" in
    deploy)
      pull_or_build
      run_migrations
      apply_release
      if ! smoke_test "${HEALTH_URL}" "health"; then
        log "health gate failed -> rolling back"
        rollback || true
        exit 1
      fi
      if ! smoke_test "${READY_URL}" "ready"; then
        log "ready gate failed (db/redis not green) -> rolling back"
        rollback || true
        exit 1
      fi
      log "deploy of ${IMAGE}:${IMAGE_TAG} complete (health + ready green)"
      ;;
    rollback)
      rollback
      ;;
    migrate)
      pull_or_build
      run_migrations
      ;;
    *)
      log "usage: $0 [deploy|rollback|migrate]"
      exit 2
      ;;
  esac
}

main "$@"
