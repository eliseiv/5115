# syntax=docker/dockerfile:1.7
# Multi-stage image for the modular monolith (07-deployment.md).
# Base: official python:3.12-slim, pinned by digest for reproducibility.
# Runtime runs as non-root. No secrets are baked in — all config via env (05-security.md).

# --- Stage 1: builder -------------------------------------------------------
# python:3.12-slim-bookworm (digest pin — update via `docker buildx imagetools inspect`).
# Digest verified to resolve to CPython 3.12.13 (02-tech-stack.md mandates 3.12.x).
FROM python:3.12-slim-bookworm@sha256:93ab4b7fa528b25124c97bcc755415e60eb671a86b4dbe0328df2fe2d1c1193d AS builder

# uv 0.4.x (02-tech-stack.md), copied from the official distroless uv image (pinned).
COPY --from=ghcr.io/astral-sh/uv:0.4.30 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

# C toolchain for native deps without prebuilt wheels (gcc + headers via build-essential).
# Builder-only: these never reach the runtime image (multi-stage; runtime stays slim).
# No exact apt patch-pin — Debian security updates drop specific patch versions and
# would break reproducible builds; the base image digest already pins the apt snapshot.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (cached layer) using the locked manifest only.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Now bring in the source and install the project itself (no dev deps in runtime image).
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- Stage 2: runtime -------------------------------------------------------
# Same digest as builder; verified CPython 3.12.13 (02-tech-stack.md).
FROM python:3.12-slim-bookworm@sha256:93ab4b7fa528b25124c97bcc755415e60eb671a86b4dbe0328df2fe2d1c1193d AS runtime

# curl is required for the container HEALTHCHECK.
# No exact apt patch-pin: Debian security updates remove specific patch versions
# (e.g. curl=7.88.1-10+deb12u8), which breaks reproducible builds. The base image
# is pinned by digest, so the apt repo snapshot is already deterministic per build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (05-security.md: minimal attack surface).
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# Copy the resolved virtualenv and application code from the builder.
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --from=builder --chown=10001:10001 /app/src /app/src
COPY --from=builder --chown=10001:10001 /app/migrations /app/migrations
COPY --from=builder --chown=10001:10001 /app/alembic.ini /app/alembic.ini

USER 10001:10001

EXPOSE 8000

# Liveness probe at container level (07-deployment.md: GET /health).
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Prod process manager: Gunicorn + UvicornWorker (02-tech-stack.md).
# Graceful shutdown: gunicorn handles SIGTERM, drains workers within --graceful-timeout.
# Worker count is env-driven: GUNICORN_WORKERS (default 4 == the previous hardcoded value, so
# existing instances are unchanged). A RAM-constrained host (e.g. novirell: shared 3.8 GB) sets a
# lower value in its .env — see deploy/novirell.env.example (GUNICORN_WORKERS=2) and
# docs/07-deployment.md §Sizing (recompute the DB pool: (pool_size+max_overflow)*workers).
#
# Worker timeout is ALSO env-driven: GUNICORN_TIMEOUT (default 90 == the previous hardcoded value, so
# existing instances are unchanged). WHY env-driven / when to raise it: a single /v1/chat/run turn
# can run 40-60s+ (image generation chains Responses->images->Responses) and the app's per-provider
# budgets go up to 180s (DEEP_THINKING_TIMEOUT_SECONDS, src/app/config.py). With UvicornWorker (ASYNC)
# gunicorn's --timeout is a WORKER-HEARTBEAT timeout, not a per-request one: uvicorn keeps notifying
# the master from the event loop while an async upstream call is in flight, so a slow ASYNC turn does
# NOT get killed at 90s (the real 502 came from nginx's 60s proxy_read_timeout — fixed in
# deploy/nginx/novirell.shop.conf). BUT to keep the timeout chain consistent and defensive
# (nginx proxy_read_timeout 300s >= gunicorn --timeout >= app provider budget <=180s) and to guard the
# brief SYNC-CPU phases of the image path (base64 decode + BYTEA insert) against ever tripping a 90s
# heartbeat kill under load, novirell sets GUNICORN_TIMEOUT=300 (deploy/novirell.env.example). Legacy
# instances keep 90 (default), so their behaviour is unchanged.
# NB: uses the `sh -c` + `exec` shell form ON PURPOSE — the JSON exec-form CMD does NOT expand
# ${...}. `exec` replaces the shell so gunicorn stays PID 1 and receives SIGTERM (graceful drain),
# exactly as the previous exec-form did.
CMD ["sh", "-c", "exec gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w ${GUNICORN_WORKERS:-4} -b 0.0.0.0:8000 --graceful-timeout 30 --timeout ${GUNICORN_TIMEOUT:-90} --access-logfile - --error-logfile -"]
