#!/usr/bin/env bash
# Deploy orchestration implementing the contract in docs/07-deployment.md.
#
# NOTE (ADR-017): this generic, registry/immutable-tag-oriented seam is NOT the active prod
# path. The fixed deploy target is shared-server + external Traefik + GitHub Actions SSH, where
# the image is BUILT ON THE SERVER (`docker compose up -d --build`, no registry). The active
# deploy is .github/workflows/deploy.yml (see infra/deploy/README.md). This file is kept only as
# a generic reference for a possible future registry-based target (would require a new ADR).
#
# Platform-neutral: the concrete target (k8s manifests / SSH+systemd / serverless) is an
# architect decision and is intentionally NOT hard-coded here. The release-apply step is a
# single seam (`apply_release`) to be specialised once the target is fixed.
#
# Steps (07-deployment.md §CI/CD 8–9, §Миграции, §Health/readiness):
#   1. resolve immutable image tag (commit SHA)
#   2. pre-deploy: alembic upgrade head  (expand/contract, backward-compatible)
#   3. rolling apply of the new image
#   4. smoke-test GET /health, then GET /ready
#   5. on failure -> rollback to the previous tag
#
# Idempotent: re-running with the same IMAGE_TAG is a no-op for migrations (alembic) and a
# converging apply for the release.
set -Eeuo pipefail

# --- inputs (all via env / args; no secrets in this file) -------------------
IMAGE="${IMAGE:?set IMAGE=ghcr.io/<org>/<repo>}"
IMAGE_TAG="${IMAGE_TAG:?set IMAGE_TAG=<git-sha>}"
HEALTH_URL="${HEALTH_URL:?set HEALTH_URL=https://<host>/health}"
READY_URL="${READY_URL:-${HEALTH_URL%/health}/ready}"
PREVIOUS_TAG="${PREVIOUS_TAG:-}"   # required only if rollback is requested
SMOKE_RETRIES="${SMOKE_RETRIES:-30}"
SMOKE_INTERVAL="${SMOKE_INTERVAL:-5}"

log() { printf '[deploy] %s\n' "$*" >&2; }

run_migrations() {
  # DATABASE_URL comes from the environment / secret manager — never embedded here.
  log "running alembic upgrade head (pre-deploy job)"
  alembic upgrade head
}

apply_release() {
  # SEAM: replace with the concrete rollout once the deploy target is decided.
  #   k8s:   kubectl set image deploy/api api="${IMAGE}:${IMAGE_TAG}" && kubectl rollout status ...
  #   SSH:   ssh host "docker pull ${IMAGE}:${IMAGE_TAG} && systemctl restart api"
  # The rollout MUST be a rolling update to honour expand/contract migrations.
  log "applying release ${IMAGE}:${IMAGE_TAG} (rolling)"
  : "${APPLY_RELEASE_CMD:?set APPLY_RELEASE_CMD to the platform rollout command (TD-005)}"
  bash -c "${APPLY_RELEASE_CMD}"
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
  # Schema is backward-compatible (expand/contract) so code rolls back without schema revert.
  IMAGE_TAG="${PREVIOUS_TAG}" apply_release
  smoke_test "${HEALTH_URL}" "health(rollback)"
}

main() {
  case "${1:-deploy}" in
    deploy)
      run_migrations
      apply_release
      if ! smoke_test "${HEALTH_URL}" "health"; then
        rollback || true
        exit 1
      fi
      smoke_test "${READY_URL}" "ready" || log "WARN: /ready not green (deps warming up?)"
      log "deploy of ${IMAGE_TAG} complete"
      ;;
    rollback)
      rollback
      ;;
    *)
      log "usage: $0 [deploy|rollback]"
      exit 2
      ;;
  esac
}

main "$@"
