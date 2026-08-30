# Project:   DFE Engine
# File:      Makefile
# Purpose:   CI targets wrapping hyperi-ci
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

.PHONY: quality test build check dev e2e-server e2e-clean

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
# Fresh slate on every start: YAML config, schema writes, minted secrets and
# artifacts live under tmp/e2e (gitignored) so the schemas/ submodule and
# ./config are never touched, and every ClickHouse database the engine creates
# is namespaced under the $(E2E_CH_PREFIX) prefix so the wipe can sweep it by
# prefix rather than by a hand-maintained list. Wiping on START (not on stop) is
# deliberate: a killed or crashed run leaves nothing behind for the next one.
# Pass E2E_KEEP=1 to reuse the last run instead of wiping.
# Playwright must poll /readyz (not /health/ready, which 404s on this binary):
#   DFE_ENGINE_READY_URL=http://127.0.0.1:8003/readyz yarn test:e2e
# Startup parity: a plain `dfe-engine run`, so the process comes up with exactly
# what auth/bootstrap.py seeds (roles.yaml, the four dfe-* groups, break-glass
# admin) and no baseline beyond it. Fixture data is the suite's job, seeded over
# the API from dfe-ui.
# Hunt log and checkpoint paths are pinned in too: they are not among the
# DFE_CONFIG_DIR-derived subdirs, so their defaults land in the CWD and would
# outlive the wipe. The gitops deploy repo is pinned for the same reason, and
# only takes effect when .env.e2e sets DFE_GITOPS_ENABLED=true -- the
# app-management seeds write their instances, artefacts and sources into it.
# NOT swept: ClickHouse users/roles, which are server-global rather than
# per-database. Only the governance reconcile creates those, and it is off
# unless DFE_ORG_PROVISIONING_ENABLED is set.
E2E_WORKSPACE := $(CURDIR)/tmp/e2e
E2E_CH_PREFIX := dfe_e2e
E2E_CH_DATA_DB := $(E2E_CH_PREFIX)
E2E_CH_HUNTS_DB := $(E2E_CH_PREFIX)_hunts
E2E_CH_AUDIT_DB := $(E2E_CH_PREFIX)_audit
E2E_CH_OTEL_DB := $(E2E_CH_PREFIX)_otel

# Drop every ClickHouse database under the e2e prefix. Enumerating the server
# beats a fixed list: a database added by a future bootstrap path is swept the
# day it appears. The prefix cannot match the shared dfe / dfe_hunts / dfe_audit
# / dfe_meta databases, so a dev stack on the same server is untouched.
define e2e-ch-wipe
	ch_host="$${DFE_CLICKHOUSE_HOST:-localhost}"; \
	ch_port="$${DFE_CLICKHOUSE_PORT:-8123}"; \
	ch_user="$${DFE_CLICKHOUSE_USERNAME:-default}"; \
	ch_pass="$${DFE_CLICKHOUSE_PASSWORD}"; \
	echo "Dropping ClickHouse $(E2E_CH_PREFIX)* on $$ch_host:$$ch_port"; \
	dbs=$$(curl -sf -u "$$ch_user:$$ch_pass" "http://$$ch_host:$$ch_port/" \
		--data-binary "SELECT name FROM system.databases WHERE name LIKE '$(E2E_CH_PREFIX)%'"); \
	rc=$$?; \
	if [ $$rc -eq 22 ]; then \
		echo "error: ClickHouse refused the wipe query (curl 22 -- HTTP 4xx/5xx)."; \
		echo "Databases from earlier runs may still exist and cannot be cleared."; \
		echo "Check DFE_CLICKHOUSE_USERNAME / DFE_CLICKHOUSE_PASSWORD in .env.e2e against"; \
		echo "the running server (dfe-docker/.env CLICKHOUSE_PASSWORD; empty is rejected)."; \
		exit 1; \
	fi; \
	if [ $$rc -ne 0 ]; then \
		echo "warning: ClickHouse unreachable (curl $$rc); nothing to sweep"; \
		dbs=""; \
	fi; \
	for db in $$dbs; do \
		curl -sf -u "$$ch_user:$$ch_pass" "http://$$ch_host:$$ch_port/" \
			--data-binary "DROP DATABASE IF EXISTS $$db" \
			&& echo "  dropped $$db" \
			|| { echo "error: could not drop $$db"; exit 1; }; \
	done
endef

# Refuse to start behind another listener on the API port. This server binds
# 0.0.0.0, so a loopback-specific bind -- the dfe-engine container publishing
# 127.0.0.1:8003 -- shadows it for localhost traffic: both binds succeed, no
# error is raised, and the browser, dfe-ui and Playwright all reach the OTHER
# process and its persistent volume. A fresh workspace here cannot show through
# that, so the collision has to be fatal rather than silent.
define e2e-port-guard
	if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$$DFE_API_PORT" -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "error: something already listens on port $$DFE_API_PORT:"; \
		lsof -nP -iTCP:"$$DFE_API_PORT" -sTCP:LISTEN; \
		echo "Stop it (e.g. docker stop dfe-engine) or pick another port:"; \
		echo "  E2E_PORT=8004 make e2e-server"; \
		exit 1; \
	fi
endef

# Teardown on demand: same wipe the next `make e2e-server` would do, for when a
# run is finished with and should leave nothing behind.
e2e-clean:
	@test -f .env.e2e || { echo "Create .env.e2e first (copy .env and set DFE_ENV=test)"; exit 1; }
	@echo "Removing $(E2E_WORKSPACE)"
	set -a; . ./.env.e2e; set +a; \
	rm -rf "$(E2E_WORKSPACE)"; \
	$(e2e-ch-wipe)

e2e-server:
	@test -f .env.e2e || { echo "Create .env.e2e first (copy .env and set DFE_ENV=test)"; exit 1; }
	@echo "e2e workspace (fresh) -> $(E2E_WORKSPACE)"
	@echo "ClickHouse databases -> $(E2E_CH_PREFIX)* (data + audit archive)"
	@echo "Swagger UI -> http://localhost:$${E2E_PORT:-8003}/docs  (select API or E2E)"
	@echo "Playwright ready URL -> DFE_ENGINE_READY_URL=http://127.0.0.1:$${E2E_PORT:-8003}/readyz"
	@echo "e2e seed -> POST http://localhost:$${E2E_PORT:-8003}/api/e2e/seed-static"
	set -a; . ./.env.e2e; set +a; \
	export DFE_API_PORT="$${E2E_PORT:-$${DFE_API_PORT:-8003}}"; \
	$(e2e-port-guard); \
	if [ -z "$(E2E_KEEP)" ]; then rm -rf "$(E2E_WORKSPACE)"; fi; \
	mkdir -p "$(E2E_WORKSPACE)/config" "$(E2E_WORKSPACE)/schemas" "$(E2E_WORKSPACE)/secrets"; \
	if [ -d schemas/common-header ]; then \
		echo "Seeding schema snapshot from submodule (writes stay in tmp/e2e)"; \
		rsync -a --exclude '.git' schemas/ "$(E2E_WORKSPACE)/schemas/"; \
	fi; \
	if [ -z "$(E2E_KEEP)" ]; then \
		$(e2e-ch-wipe); \
	fi; \
	export DFE_CONFIG_DIR="$(E2E_WORKSPACE)/config"; \
	export DFE_SCHEMAS_DIR="$(E2E_WORKSPACE)/schemas"; \
	export DFE_SECRETS_PATH="$(E2E_WORKSPACE)/secrets"; \
	export DFE_STORAGE_PATH="$(E2E_WORKSPACE)/artifacts"; \
	export DFE_HUNT_LOG_PATH="$(E2E_WORKSPACE)/hunt-log"; \
	export DFE_HUNTS_CHECKPOINT_PATH="$(E2E_WORKSPACE)/hunt-checkpoints"; \
	export DFE_GITOPS_LOCAL_PATH="$(E2E_WORKSPACE)/deploy-repo"; \
	export DFE_CLICKHOUSE_DATA_DATABASE="$(E2E_CH_DATA_DB)"; \
	export DFE_E2E_SERVER=true; \
	export DFE_ENV_FILE=.env.e2e; \
	uv run dfe-engine run
