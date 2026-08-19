"""Export OpenAPI spec from FastAPI app to JSON.

Run after any model/route changes::

    uv run python openapi-spec/generate.py

The output ``openapi.json`` is committed to the repo.
Frontend devs use it for:
- TypeScript type generation (openapi-typescript)
- Mock API server (Prism)
- Contract validation in CI
"""

import json
import os
from pathlib import Path

from dfe_engine.api.app import create_app
from dfe_engine.settings import load_settings

SPEC_DIR = Path(__file__).parent
SPEC_FILE = SPEC_DIR / "openapi.json"


def main() -> None:
    # Schema export only — avoid production posture guard on dev JWT placeholder.
    os.environ["DFE_ENV"] = "dev"
    settings = load_settings()
    app = create_app(settings=settings)

    # Health probes are excluded from the schema at their include
    # (include_in_schema=False in app.py); nothing to strip here. Clear any
    # cached schema so it regenerates.
    app.openapi_schema = None

    spec = app.openapi()
    if any(path.startswith("/api/v1/e2e") for path in spec.get("paths", {})):
        raise SystemExit("refusing to commit e2e-server paths into openapi.json")
    SPEC_FILE.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"OpenAPI spec written to {SPEC_FILE} ({len(spec.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
