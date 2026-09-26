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

# The release commit-back keeps this at the released number, and the committed-spec
# test compares against it, so the generator stamps from the same file.
VERSION_FILE = SPEC_DIR.parent / "VERSION"

# pyproject ships 0.0.0 and semantic-release stamps the real number in CI, so an
# editable install reports a placeholder rather than the version it is describing.
PLACEHOLDER_VERSIONS = frozenset({"", "dev", "0.0.0"})


def _dump(path: Path, spec: dict) -> None:
    path.write_text(json.dumps(spec, indent=2) + "\n")


def _repo_version() -> str:
    """The VERSION file's number, or empty when the file is missing."""
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return ""


def _spec_version(reported: str) -> str:
    """The version to stamp, refusing the placeholder that a dev tree reports.

    A generator run on a machine whose install carries no version wrote 0.0.0
    over 1.8.0 and every consumer of the spec inherited it, so the placeholder is
    a hard failure here rather than a value to commit.
    """
    if reported not in PLACEHOLDER_VERSIONS:
        return reported
    version = _repo_version()
    if version not in PLACEHOLDER_VERSIONS:
        return version
    raise SystemExit(
        f"refusing to write info.version={reported!r} into the committed spec: "
        f"{VERSION_FILE} carries no release number either"
    )


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
    version = _spec_version(spec.get("info", {}).get("version", ""))
    spec["info"]["version"] = version
    _dump(SPEC_FILE, spec)
    print(f"OpenAPI spec written to {SPEC_FILE} ({len(spec.get('paths', {}))} paths, v{version})")

    e2e_spec = build_e2e_spec(version=version)
    _dump(E2E_SPEC_FILE, e2e_spec)
    print(f"E2E OpenAPI spec written to {E2E_SPEC_FILE} ({len(e2e_spec.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
