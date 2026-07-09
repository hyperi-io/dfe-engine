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
from pathlib import Path

from dfe_engine.api.app import create_app

SPEC_DIR = Path(__file__).parent
SPEC_FILE = SPEC_DIR / "openapi.json"


def main() -> None:
    app = create_app()

    # Health probes are excluded from the schema at their include
    # (include_in_schema=False in app.py); nothing to strip here. Clear any
    # cached schema so it regenerates.
    app.openapi_schema = None

    spec = app.openapi()
    SPEC_FILE.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"OpenAPI spec written to {SPEC_FILE} ({len(spec.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
