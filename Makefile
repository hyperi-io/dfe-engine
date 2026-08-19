# Project:   DFE Engine
# File:      Makefile
# Purpose:   CI targets wrapping hyperi-ci
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

.PHONY: quality test build check dev e2e-server

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
	uv run dfe-engine run

# Persisted host-run API for Playwright (dfe-ui `yarn test:e2e`).
# Fresh workspace each start: YAML config, schema writes, and minted secrets
# live under tmp/e2e (gitignored) so the schemas/ submodule and ./config are
# never touched. ClickHouse uses dedicated dfe_e2e / dfe_e2e_hunts databases
# (created on bootstrap; dropped on each fresh start so the docker `dfe` DB
# is left alone). Pass E2E_KEEP=1 to reuse the last run instead of wiping.
# Playwright must poll /readyz (not /health/ready, which 404s on this binary):
#   DFE_ENGINE_READY_URL=http://127.0.0.1:8003/readyz yarn test:e2e
E2E_WORKSPACE := $(CURDIR)/tmp/e2e
E2E_CH_DATA_DB := dfe_e2e
E2E_CH_HUNTS_DB := dfe_e2e_hunts

e2e-server:
	@test -f .env.e2e || { echo "Create .env.e2e first (copy .env and set DFE_ENV=test)"; exit 1; }
	@echo "e2e workspace (fresh) -> $(E2E_WORKSPACE)"
	@echo "ClickHouse databases -> $(E2E_CH_DATA_DB), $(E2E_CH_HUNTS_DB)"
	@echo "Swagger UI -> http://localhost:$${DFE_API_PORT:-8003}/docs"
	@echo "Playwright ready URL -> DFE_ENGINE_READY_URL=http://127.0.0.1:$${DFE_API_PORT:-8003}/readyz"
	@echo "e2e seed-admin -> POST http://localhost:$${DFE_API_PORT:-8003}/api/v1/e2e/seed-admin"
	set -a; . ./.env.e2e; set +a; \
	if [ -z "$(E2E_KEEP)" ]; then rm -rf "$(E2E_WORKSPACE)"; fi; \
	mkdir -p "$(E2E_WORKSPACE)/config" "$(E2E_WORKSPACE)/schemas" "$(E2E_WORKSPACE)/secrets"; \
	if [ -d schemas/common-header ]; then \
		echo "Seeding schema snapshot from submodule (writes stay in tmp/e2e)"; \
		rsync -a --exclude '.git' schemas/ "$(E2E_WORKSPACE)/schemas/"; \
	fi; \
	if [ -z "$(E2E_KEEP)" ]; then \
		ch_host="$${DFE_CLICKHOUSE_HOST:-localhost}"; \
		ch_port="$${DFE_CLICKHOUSE_PORT:-8123}"; \
		echo "Dropping ClickHouse $(E2E_CH_DATA_DB) / $(E2E_CH_HUNTS_DB) on $$ch_host:$$ch_port"; \
		curl -sf -u "$${DFE_CLICKHOUSE_USERNAME:-default}:$${DFE_CLICKHOUSE_PASSWORD}" \
			"http://$$ch_host:$$ch_port/" \
			--data-binary "DROP DATABASE IF EXISTS $(E2E_CH_DATA_DB)" \
			|| echo "warning: could not drop $(E2E_CH_DATA_DB) (is ClickHouse up?)"; \
		curl -sf -u "$${DFE_CLICKHOUSE_USERNAME:-default}:$${DFE_CLICKHOUSE_PASSWORD}" \
			"http://$$ch_host:$$ch_port/" \
			--data-binary "DROP DATABASE IF EXISTS $(E2E_CH_HUNTS_DB)" \
			|| echo "warning: could not drop $(E2E_CH_HUNTS_DB)"; \
	fi; \
	export DFE_CONFIG_DIR="$(E2E_WORKSPACE)/config"; \
	export DFE_SCHEMAS_DIR="$(E2E_WORKSPACE)/schemas"; \
	export DFE_SECRETS_PATH="$(E2E_WORKSPACE)/secrets"; \
	export DFE_STORAGE_PATH="$(E2E_WORKSPACE)/artifacts"; \
	export DFE_CLICKHOUSE_DATA_DATABASE="$(E2E_CH_DATA_DB)"; \
	export DFE_CLICKHOUSE_HUNTS_DATABASE="$(E2E_CH_HUNTS_DB)"; \
	export DFE_E2E_SERVER=true; \
	export DFE_ENV_FILE=.env.e2e; \
	uv run dfe-engine run
