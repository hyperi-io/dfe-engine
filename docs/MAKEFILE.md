# Local Dev Tooling — Makefile + Pre-commit

Two things to set up for the team:

1. **Makefile** — standard dev commands (start, test, lint)
2. **Pre-commit hook** — auto-regenerate `openapi-spec/openapi.json` when the API changes

---

## 1. Makefile

Create `Makefile` at the project root:

```makefile
.PHONY: serve mock dev test lint spec install

# Start the API server (hot-reload via uvicorn --reload)
serve:
	dfe-engine run

# Start Prism mock server (for dfe-ui dev — no running Python needed)
mock:
	docker compose -f docker-compose.dev.yaml up

# Full dev environment: mock in background, API in foreground
dev:
	docker compose -f docker-compose.dev.yaml up -d
	dfe-engine run

# Run test suite
test:
	python -m pytest -q

# Lint + format check
lint:
	ruff check src tests
	ruff format --check src tests

# Regenerate openapi-spec/openapi.json from the live FastAPI app
spec:
	python scripts/export_openapi.py

# Install dev dependencies
install:
	uv sync
```

> Makefile indentation **must be tabs**, not spaces. If your editor converts tabs to spaces, fix it for this file only.

---

## 2. OpenAPI Export Script

Create `scripts/export_openapi.py` (Makefile `spec` target calls this; pre-commit hook does too):

```python
#!/usr/bin/env python3
# Project:   dfe-engine
# File:      scripts/export_openapi.py
# Purpose:   Regenerate openapi-spec/openapi.json from the FastAPI app
# Language:  Python

import json
import sys
from pathlib import Path

# Ensure src/ is on the path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dfe_engine.api.app import create_app

OUTPUT = Path(__file__).parent.parent / "openapi-spec" / "openapi.json"

app = create_app()
schema = app.openapi()

OUTPUT.write_text(json.dumps(schema, indent=2) + "\n")
print(f"Written: {OUTPUT}")
```

---

## 3. Pre-commit Hook

Pre-commit keeps `openapi-spec/openapi.json` in sync automatically. If the spec is out of date when you commit, the hook fails — regenerate, re-stage, and commit again.

### Install pre-commit

```bash
uv pip install pre-commit
pre-commit install
```

### Create `.pre-commit-config.yaml` at the project root

```yaml
repos:
  - repo: local
    hooks:
      - id: openapi-spec-sync
        name: Regenerate OpenAPI spec
        language: python
        entry: python scripts/export_openapi.py
        files: 'src/dfe_engine/api/.*\.py$'
        pass_filenames: false
        additional_dependencies: []
```

This hook fires when any file under `src/dfe_engine/api/` changes. It regenerates the spec — if `openapi-spec/openapi.json` changed, git sees it as unstaged and the commit is blocked. Stage the updated file and commit again.

### Typical workflow

```bash
# Add a new route to src/dfe_engine/api/v1/hunts.py
git add src/dfe_engine/api/v1/hunts.py
git commit -m "feat: add hunts router"

# → hook fires, regenerates openapi-spec/openapi.json
# → if spec changed, commit is blocked with message like:
#    "Regenerate OpenAPI spec...Failed"

# Stage the updated spec and retry
git add openapi-spec/openapi.json
git commit -m "feat: add hunts router"
# → commit succeeds
```

---

## 4. Add pre-commit to dev dependencies

In `pyproject.toml`, under `[dependency-groups]`:

```toml
[dependency-groups]
dev = [
    "pytest>=...",
    "pre-commit>=4.0",   # add this line
    ...
]
```

Then sync:

```bash
uv sync
```

---

## 5. New dev setup (onboarding)

After cloning:

```bash
git submodule update --init --recursive
uv sync
pre-commit install
make dev   # or: make mock (background) + make serve (foreground)
```

---

## What's NOT in the Makefile

- **Build / publish / container** — handled by HyperI CI (`ci/` submodule). Don't duplicate here.
- **Database migrations** — dfe-engine uses YAML SSoT, no migrations.
- **Deployment** — Argo CD handles this via Helm values.
