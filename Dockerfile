# Project:   dfe-engine
# File:      Dockerfile
# Purpose:   Container image for the dfe-engine API server
# Language:  Docker
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

# Builder: install all Python deps into a venv using uv + locked dependencies.
# The private hyperi-pylib index requires Artifactory credentials at build time
# only — they are not present in the runtime image.
FROM python:3.12-slim AS builder

ARG ARTIFACTORY_CI_USERNAME
ARG ARTIFACTORY_CI_TOKEN

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency manifest first so Docker can cache the install layer
# independently of source code changes.
COPY pyproject.toml uv.lock ./
COPY src/ src/

# UV_INDEX_HYPERI_REPO_* provides credentials for the private hyperi-pylib
# index declared in pyproject.toml [[tool.uv.index]].
RUN UV_INDEX_HYPERI_REPO_USERNAME=${ARTIFACTORY_CI_USERNAME} \
    UV_INDEX_HYPERI_REPO_PASSWORD=${ARTIFACTORY_CI_TOKEN} \
    uv sync --no-dev --frozen

# Runtime: minimal image, no build tools, no Artifactory credentials.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --uid 1000 appuser

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8000/api/v1/system/health > /dev/null || exit 1

ENTRYPOINT ["dfe-api"]
CMD ["run"]
