# Project:   dfe-engine
# File:      Dockerfile
# Purpose:   production container image
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# The runtime stage follows the scalo-py deployment contract, and
# tests/unit/test_deployment/test_contract.py guards it.

# --- Builder stage ---
# Trixie in both stages, so the runtime glibc is never older than the builder's.
ARG BUILDER_IMAGE=ghcr.io/astral-sh/uv:python3.14-trixie-slim@sha256:d8e732839dfc4225a1ff698803b478b2223c05636c055d3add41ab9dbec0f315
FROM ${BUILDER_IMAGE} AS builder

WORKDIR /app

# Compile .pyc once here rather than on every cold start.
ENV UV_COMPILE_BYTECODE=1

# Dependencies first, so a source edit does not reinstall them. UV_NO_BUILD=1
# fails a dependency with no wheel, and only here: uv builds the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN UV_NO_BUILD=1 uv sync --frozen --no-dev --no-install-project

COPY src/ src/
RUN uv sync --frozen --no-dev

# The schema seed bootstrap.py copies on first run (README.md, Configuration), and
# the writable dirs the runtime UID owns. /app/app-config takes the app configs a
# deployment with no chart has the engine render.
RUN /app/.venv/bin/python -c \
    "import dfe_schemas, shutil; shutil.copytree(dfe_schemas.schemas_root(), '/app/schemas-seed')" \
    && mkdir -p /app/schemas /app/config /app/secrets /app/app-config \
    && chown -R 1000:1000 /app/schemas /app/config /app/secrets /app/app-config

# --- Runtime stage ---
# A literal, not an ARG: test_contract.py matches `FROM <base>` as written.
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS runtime

LABEL org.opencontainers.image.title="dfe-engine"
LABEL org.opencontainers.image.description="DFE Engine -- REST API and config control plane"
LABEL org.opencontainers.image.vendor="HYPERI PTY LIMITED"
LABEL org.opencontainers.image.licenses="BUSL-1.1"
LABEL io.hyperi.profile="production"

# Unpinned on purpose: trixie point releases drop superseded versions, so a pin breaks the next rebuild.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl netcat-openbsd iputils-ping \
    && rm -rf /var/lib/apt/lists/*
ARG OCI_SOURCE=""
ARG OCI_REVISION=""
ARG OCI_VERSION=""
ARG OCI_CREATED=""
LABEL org.opencontainers.image.source="${OCI_SOURCE}"
LABEL org.opencontainers.image.revision="${OCI_REVISION}"
LABEL org.opencontainers.image.version="${OCI_VERSION}"
LABEL org.opencontainers.image.created="${OCI_CREATED}"

COPY --from=builder /app /app
# The hyperi-ci container build adds /licenses/LICENSE and the Build's dist/THIRD-PARTY-NOTICES to this final stage.
COPY LICENSE NOTICE /app/
ENV PATH="/app/.venv/bin:$PATH"

# The relative secrets default resolves here, not under an unwritable /.
WORKDIR /app

# DFE_ENGINE_SECRETS_PATH holds a directory path, not a secret value, and the engine and chart read it by name.
# hadolint ignore=DL3064
ENV DFE_CONFIG_DIR=/app/config \
    DFE_ENGINE_SCHEMAS_DIR=/app/schemas \
    DFE_ENGINE_SECRETS_PATH=/app/secrets

RUN useradd --create-home --uid 1000 appuser
USER 1000

EXPOSE 9090 8000

# 9090 serves health and /metrics, 8000 the API.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["curl", "-sf", "-o", "/dev/null", "http://localhost:9090/livez"]

ENTRYPOINT ["dfe-engine"]
CMD ["run"]
