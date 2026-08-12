# DataOpsStudio 2.0 — backend image (on-prem Docker form, design §1.2).
#
# Single image, dual purpose: the API process and the worker process share the
# same code and dependencies; compose picks one with `command:` (python -m app.main
# vs python -m app.worker). The same image also runs migrations / bootstrap
# (python -m app.launcher ...) in the init service.
#
# Build:  docker build -t dataops-studio:2.0 .
# Run:    see docker/compose.dataops.yml
#
# Design notes:
# - python:3.12-slim matches contract §1 (Python 3.12.x; launcher.doctor checks 3.12).
# - uv installs from the committed uv.lock so the image is reproducible (same lock
#   the portable form uses).
# - Non-root user `dataops` (uid 10001) — initdb/portable docs require a real
#   non-root user; the metadata PG runs in its own official image, so this image
#   never needs PG server binaries.
# - No driver of secrets baked in: secrets are mounted at runtime into a shared
#   config volume by the init service (see compose), never COPYed.

# ── Stage 1: build the virtualenv with uv ─────────────────────────────────────
FROM python:3.12-slim AS builder

# uv: pinned, copied from the official distroless uv image (no curl|sh).
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Resolve dependencies first (cache layer): only lock + manifest, no app code.
# --no-install-project so a source change does not bust the dependency layer.
# --no-dev: runtime image excludes ruff/mypy/pytest groups.
# .python-version (3.12) is honored against the base image's interpreter
# (UV_PYTHON_DOWNLOADS=0 forbids fetching a different one).
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Now add the application source and install the project itself into the venv.
COPY app ./app
COPY alembic.ini ./alembic.ini
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ── Stage 2: lean runtime ─────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ARG DATAOPS_BUILD_VERSION=2.0.1
ARG DATAOPS_BUILD_COMMIT=unknown
ARG DATAOPS_IMAGE_VERSION=2.0.1

# libpq runtime is bundled by psycopg[binary]; only need a CA bundle + tini for
# correct PID-1 signal forwarding (graceful worker drain on SIGTERM).
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# Real non-root user (must exist in /etc/passwd — see quickstart note).
RUN groupadd --gid 10001 dataops \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin dataops

WORKDIR /app

# Copy the resolved venv and the app from the builder.
COPY --from=builder --chown=dataops:dataops /app/.venv /app/.venv
COPY --from=builder --chown=dataops:dataops /app/app /app/app
COPY --from=builder --chown=dataops:dataops /app/alembic.ini /app/alembic.ini
COPY --from=builder --chown=dataops:dataops /app/pyproject.toml /app/pyproject.toml

# Put the venv on PATH so `python -m app.main` resolves the locked interpreter.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATAOPS_BUILD_VERSION="${DATAOPS_BUILD_VERSION}" \
    DATAOPS_BUILD_COMMIT="${DATAOPS_BUILD_COMMIT}" \
    DATAOPS_IMAGE_VERSION="${DATAOPS_IMAGE_VERSION}"

LABEL org.opencontainers.image.version="${DATAOPS_BUILD_VERSION}" \
      org.opencontainers.image.revision="${DATAOPS_BUILD_COMMIT}"

USER dataops

# tini reaps zombies and forwards SIGTERM so the worker's graceful-drain handler
# (app/worker.py) and uvicorn shutdown run on `docker stop`.
ENTRYPOINT ["tini", "--"]

# Default to the API; compose overrides command: for worker / init.
CMD ["python", "-m", "app.main"]
