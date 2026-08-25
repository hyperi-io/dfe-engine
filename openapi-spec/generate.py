"""Export OpenAPI spec from FastAPI app to JSON.

Run after any model/route changes::

    uv run python openapi-spec/generate.py

Writes:
- ``openapi.json`` — product API (committed; frontend types, CLI, Prism)
- ``openapi.e2e.json`` — Playwright helpers for ``make e2e-server``
"""

import json
import os
from pathlib import Path

from dfe_engine.api.app import create_app
from dfe_engine.api.e2e_docs import build_e2e_spec
from dfe_engine.settings import load_settings

SPEC_DIR = Path(__file__).parent
SPEC_FILE = SPEC_DIR / "openapi.json"
E2E_SPEC_FILE = SPEC_DIR / "openapi.e2e.json"


def _dump(path: Path, spec: dict) -> None:
    path.write_text(json.dumps(spec, indent=2) + "\n")


def main() -> None:
    # Schema export only — avoid production posture guard on dev JWT placeholder.
    os.environ["DFE_ENV"] = "dev"
    settings = load_settings()
    if settings.e2e_server:
        settings = settings.model_copy(update={"e2e_server": False})
    app = create_app(settings=settings)

    # Health probes are excluded from the schema at their include
    # (include_in_schema=False in app.py); nothing to strip here. Clear any
    # cached schema so it regenerates.
    app.openapi_schema = None

    spec = app.openapi()
    if any(path.startswith("/api/e2e") for path in spec.get("paths", {})):
        raise SystemExit("refusing to commit e2e-server paths into openapi.json")
    _dump(SPEC_FILE, spec)
    print(f"OpenAPI spec written to {SPEC_FILE} ({len(spec.get('paths', {}))} paths)")

    e2e_spec = build_e2e_spec(version=spec.get("info", {}).get("version", "dev"))
    _dump(E2E_SPEC_FILE, e2e_spec)
    print(f"E2E OpenAPI spec written to {E2E_SPEC_FILE} ({len(e2e_spec.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
