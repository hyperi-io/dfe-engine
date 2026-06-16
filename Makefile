# Project:   DFE Engine
# File:      Makefile
# Purpose:   CI targets wrapping hyperi-ci
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

.PHONY: quality test build check dev

quality:
	hyperi-ci run quality

test:
	hyperi-ci run test

build:
	hyperi-ci run build

check:
	hyperi-ci check

# Local dev API server. First time: `git submodule update --init` then
# `cp .env.example .env`. Loads .env (the app does not read it itself),
# defaults the config/schemas dirs, and serves Swagger UI at /docs.
dev:
	@test -f .env || { echo "Create .env first: cp .env.example .env"; exit 1; }
	@echo "Swagger UI -> http://localhost:$${DFE_API_PORT:-8003}/docs"
	set -a; . ./.env; set +a; \
	: "$${DFE_CONFIG_DIR:=./config}"; export DFE_CONFIG_DIR; \
	: "$${DFE_SCHEMAS_DIR:=./schemas}"; export DFE_SCHEMAS_DIR; \
	uv run dfe-api run
