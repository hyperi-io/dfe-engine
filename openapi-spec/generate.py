"""Export OpenAPI spec from FastAPI app to JSON.

Run after any model/route changes::

    uv run python openapi-spec/generate.py

Writes:
- ``openapi.json`` — product API (committed; frontend types, CLI, Prism)
- ``openapi.e2e.json`` — Playwright helpers for ``make e2e-server``
"""

import json
import os
import subprocess
from pathlib import Path

from dfe_engine.api.app import create_app
from dfe_engine.api.e2e_docs import build_e2e_spec
from dfe_engine.settings import load_settings

SPEC_DIR = Path(__file__).parent
SPEC_FILE = SPEC_DIR / "openapi.json"
E2E_SPEC_FILE = SPEC_DIR / "openapi.e2e.json"

# pyproject ships 0.0.0 and semantic-release stamps the real number in CI, so an
# editable install reports a placeholder rather than the version it is describing.
PLACEHOLDER_VERSIONS = frozenset({"", "dev", "0.0.0"})


def _dump(path: Path, spec: dict) -> None:
    path.write_text(json.dumps(spec, indent=2) + "\n")


def _latest_release_tag() -> str:
    """Newest ``vN.N.N`` tag reachable from HEAD, or empty when there is none."""
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
            cwd=SPEC_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return out.stdout.strip().lstrip("v") if out.returncode == 0 else ""


def _spec_version(reported: str) -> str:
    """The version to stamp, refusing the placeholder that a dev tree reports.

    A generator run on a machine whose install carries no version wrote 0.0.0
    over 1.8.0 and every consumer of the spec inherited it, so the placeholder is
    a hard failure here rather than a value to commit.
    """
    if reported not in PLACEHOLDER_VERSIONS:
        return reported
    tag = _latest_release_tag()
    if tag:
        return tag
    raise SystemExit(
        f"refusing to write info.version={reported!r} into the committed spec: "
        "no release tag to fall back on -- run `git fetch --tags`, or generate "
        "from an install that carries the real version"
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
