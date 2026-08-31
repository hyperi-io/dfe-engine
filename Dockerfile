# Project:   dfe-engine
# File:      Dockerfile
# Purpose:   production container image
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# The runtime stage below aligns with the hyperi-pylib deployment contract
# (src/dfe_engine/deployment_contract.py). validate_dockerfile in
# tests/unit/test_deployment/test_contract.py guards drift (runtime base image,
# EXPOSE, venv PATH, entrypoint). The builder stage is project-specific (uv venv
# build from the public PyPI index).

# --- Builder stage (uv venv) ---
# Base image is a build-arg with a digest-pinned default (#106). Trixie both
# ends so glibc(runtime) >= glibc(builder); pinned so neither tag can float and
# invert that relationship silently. Re-resolve digests on a bump; Renovate
# maintains them.
ARG BUILDER_IMAGE=ghcr.io/astral-sh/uv:python3.12-trixie-slim@sha256:36cdfbf910c8b0f651355c013e7ece9678f4ecbf030a9fd9e6779de421189805
FROM ${BUILDER_IMAGE} AS builder

WORKDIR /app

# Compile .pyc at build so the cost is paid once here, not on every cold start
# (#106). ~1s off startup for a larger image; the platform ordering ranks
# startup first, image disk last.
ENV UV_COMPILE_BYTECODE=1

# Phase 1 -- dependencies only, so a source edit does not reinstall the graph
# (#106). UV_NO_BUILD=1 requires a prebuilt wheel for every third-party dep
# (#106: a silent compile-from-source is the failure mode we most want to be
# loud); scoped to this phase because a blanket ENV breaks uv's own editable
# self-install (dfe-engine has no wheel of its own). README.md is read during
# metadata resolution (pyproject sets readme = "README.md").
COPY pyproject.toml uv.lock README.md ./
RUN UV_NO_BUILD=1 uv sync --frozen --no-dev --no-install-project

# Phase 2 -- our own source + the editable project install (built, not a wheel).
COPY src/ src/
RUN uv sync --frozen --no-dev

# /app/schemas-seed is copied out of the installed dfe-schemas wheel, so the
# image ships exactly the version uv.lock pins; bootstrap.py copies it into
# DFE_SCHEMAS_DIR on the first run of each engine version. It stays root-owned
# and read-only, as the COPY it replaced was.
#
# The rest are writable dirs owned by the runtime UID. /app/secrets is the
# scalo.secrets root (#106): the engine mints an ES384 JWT signing key on first
# boot and, with no writable secrets dir and no runtime WORKDIR, it defaulted to
# ./.secrets under / and crash-looped as non-root. COPY --from preserves this
# ownership into the runtime stage.
RUN /app/.venv/bin/python -c \
    "import dfe_schemas, shutil; shutil.copytree(dfe_schemas.schemas_root(), '/app/schemas-seed')" \
    && mkdir -p /app/schemas /app/config /app/secrets \
    && chown -R 1000:1000 /app/schemas /app/config /app/secrets

# --- Runtime stage (aligned with hyperi-pylib deployment contract) ---
# Digest-pinned runtime base (#106). python:3.12-slim is already Debian 13
# trixie. Kept literal (not an ARG) because scalo's validate_dockerfile matches
# `FROM <base>` by substring, so ARG-parameterising the runtime base needs a
# scalo-py change (hyperi-io/scalo-py#4) -- tracked, not worked around by deleting
# the drift test.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS runtime

# Static OCI labels (from contract)
LABEL org.opencontainers.image.title="dfe-engine"
LABEL org.opencontainers.image.description="DFE Engine -- REST API and config control plane"
LABEL org.opencontainers.image.vendor="HYPERI PTY LIMITED"
LABEL org.opencontainers.image.licenses="BUSL-1.1"
LABEL io.hyperi.profile="production"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl netcat-openbsd iputils-ping \
    && rm -rf /var/lib/apt/lists/*
# Dynamic OCI labels (injected by CI at build time)
ARG OCI_SOURCE=""
ARG OCI_REVISION=""
ARG OCI_VERSION=""
ARG OCI_CREATED=""
LABEL org.opencontainers.image.source="${OCI_SOURCE}"
LABEL org.opencontainers.image.revision="${OCI_REVISION}"
LABEL org.opencontainers.image.version="${OCI_VERSION}"
LABEL org.opencontainers.image.created="${OCI_CREATED}"

# Copy the uv-built virtualenv from the builder and put it on PATH.
COPY --from=builder /app /app
ENV PATH="/app/.venv/bin:$PATH"

# Runtime WORKDIR (#106): without it CWD is / and any relative default path
# (e.g. the secrets root) resolves under /, unwritable by the non-root user.
WORKDIR /app

ENV DFE_CONFIG_DIR=/app/config \
    DFE_SCHEMAS_DIR=/app/schemas \
    DFE_SECRETS_PATH=/app/secrets

RUN useradd --create-home --uid 1000 appuser
USER appuser

# 9090 = observability (health + /metrics, scalo ServiceApp); 8000 = API traffic.
EXPOSE 9090 8000

# Probe the observability port (#106): health answers on 9090, not 8000.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -sf http://localhost:9090/livez > /dev/null || exit 1

ENTRYPOINT ["dfe-engine"]
CMD ["run"]
