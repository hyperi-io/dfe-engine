"""The committed OpenAPI spec must describe the app it ships with.

``openapi-spec/openapi.json`` is not documentation. The sync workflow copies it
verbatim into dfe-ui and runs ``yarn generate`` over it, so the UI's TypeScript
types are whatever this file says -- a stale spec compiles the UI against a
contract the server does not implement. The workflow triggers on CHANGES to the
file, so a spec that is never regenerated never fires the sync either: the drift
is silent at both ends.

``dfe-api`` is NOT a consumer: it builds its command tree from
``create_app().openapi()`` live, so it is immune to this file going stale
(``dfe_engine/cli/auto/spec.py``). dfe-ui is the one that inherits the drift.

``info.version`` is dropped from the STRUCTURAL comparison deliberately. It comes
from package metadata, which is the release version in a built artefact and
``0.0.0`` in an editable install, so comparing it there would fail every local
run while proving nothing -- it generates no TypeScript.

The version is asserted separately, against the ``VERSION`` file, which reads the
same in both. Two things are checked: that the committed value is not the
placeholder (a generator run on a versionless machine wrote ``0.0.0`` over
``1.8.0`` and shipped it), and that it equals the repo's version (the specs sat
at ``1.20.19`` against a ``VERSION`` of ``1.21.2`` with every test green).

Regenerate with ``uv run python openapi-spec/generate.py``.
"""

import json
from pathlib import Path

import pytest

from dfe_engine.api.app import create_app
from dfe_engine.settings import load_settings, reset_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_DIR = REPO_ROOT / "openapi-spec"
SPEC_FILE = SPEC_DIR / "openapi.json"
E2E_SPEC_FILE = SPEC_DIR / "openapi.e2e.json"

# Read from the file, never from package metadata: an editable install reports
# 0.0.0, which is why the structural comparison drops the version at all.
VERSION_FILE = REPO_ROOT / "VERSION"

REGENERATE = "regenerate with: uv run python openapi-spec/generate.py"

# What an editable install reports for a package whose version CI stamps at release.
PLACEHOLDER_VERSIONS = {"", "dev", "0.0.0"}


@pytest.fixture(autouse=True)
def _clean_settings():
    reset_settings()
    yield
    reset_settings()


def _drop_version(spec: dict) -> dict:
    """The one field allowed to differ between a build and a working tree."""
    return {**spec, "info": {k: v for k, v in spec["info"].items() if k != "version"}}


@pytest.fixture
def generated(monkeypatch) -> dict:
    """The spec the app produces now, built exactly as generate.py builds it."""
    monkeypatch.setenv("DFE_ENV", "dev")
    settings = load_settings()
    if settings.e2e_server:
        settings = settings.model_copy(update={"e2e_server": False})
    app = create_app(settings=settings)
    app.openapi_schema = None
    return app.openapi()


@pytest.fixture
def committed() -> dict:
    return json.loads(SPEC_FILE.read_text())


def test_committed_spec_has_the_same_operations(committed, generated):
    """A route added or removed without regenerating leaves the UI a wrong client."""
    in_app = {
        f"{method.upper()} {path}" for path, ops in generated["paths"].items() for method in ops
    }
    in_spec = {
        f"{method.upper()} {path}" for path, ops in committed["paths"].items() for method in ops
    }

    undocumented = sorted(in_app - in_spec)
    phantom = sorted(in_spec - in_app)

    if undocumented or phantom:
        pytest.fail(
            f"openapi.json is stale -- {REGENERATE}\n"
            f"  in the app, missing from the spec ({len(undocumented)}): {undocumented}\n"
            f"  in the spec, absent from the app ({len(phantom)}): {phantom}"
        )


def _differences(want, have, prefix: str = "", depth: int = 3) -> list[str]:
    """Dotted paths to what differs.

    Descends far enough to name the schema or route at fault rather than
    reporting that ``components`` differs, which is true of any change at all.
    """
    if want == have:
        return []
    if depth == 0 or not (isinstance(want, dict) and isinstance(have, dict)):
        return [prefix or "<whole document>"]

    out: list[str] = []
    for key in sorted(set(want) | set(have)):
        if want.get(key) == have.get(key):
            continue
        child = f"{prefix}.{key}" if prefix else key
        if key not in want:
            out.append(f"{child} (in the spec, not in the app)")
        elif key not in have:
            out.append(f"{child} (in the app, not in the spec)")
        else:
            out.extend(_differences(want[key], have[key], child, depth - 1))
    return out


def test_committed_spec_matches_the_app_contract(committed, generated):
    """Catches the silent half: a renamed field or changed shape on a route that stays."""
    want = _drop_version(generated)
    have = _drop_version(committed)

    diffs = _differences(want, have)
    if not diffs:
        return

    shown = "\n".join(f"  {d}" for d in diffs[:15])
    more = f"\n  ... and {len(diffs) - 15} more" if len(diffs) > 15 else ""
    pytest.fail(f"openapi.json no longer matches the app -- {REGENERATE}\n{shown}{more}")


@pytest.mark.parametrize("spec_file", [SPEC_FILE, E2E_SPEC_FILE], ids=["product", "e2e"])
def test_committed_spec_carries_a_real_version(spec_file):
    """dfe-ui and Prism read this number; the placeholder tells them nothing."""
    version = json.loads(spec_file.read_text())["info"]["version"]

    assert version not in PLACEHOLDER_VERSIONS, (
        f"{spec_file.name} carries the placeholder version {version!r} -- it was "
        f"generated on a machine with no package version. {REGENERATE}"
    )


@pytest.mark.parametrize("spec_file", [SPEC_FILE, E2E_SPEC_FILE], ids=["product", "e2e"])
def test_committed_spec_version_matches_the_repo(spec_file):
    """A spec left behind at an older version takes dfe-ui's generated types with it.

    Compared against the VERSION file rather than package metadata, which reads
    0.0.0 in an editable install and would fail every local run.
    """
    committed_version = json.loads(spec_file.read_text())["info"]["version"]
    repo_version = VERSION_FILE.read_text().strip()

    assert committed_version == repo_version, (
        f"{spec_file.name} is at {committed_version!r} while VERSION says "
        f"{repo_version!r} -- the spec was not regenerated. {REGENERATE}"
    )


def test_post_sources_409_documents_the_core_landing_conflict(committed):
    """POST /sources can 409 for the core landing's reserved name, not just a plain dup.

    Regression for #381: the union once named only the plain-duplicate and
    match-conflict shapes, so a caller reading the spec could not tell the two
    "code": "conflict" causes apart.
    """
    schema = committed["paths"]["/api/v1/sources"]["post"]["responses"]["409"]["content"][
        "application/json"
    ]["schema"]
    refs = {member["$ref"] for member in schema["anyOf"]}

    assert refs == {
        "#/components/schemas/SourceNameConflictErrorResponse",
        "#/components/schemas/CoreResourceConflictErrorResponse",
        "#/components/schemas/MatchConflictErrorResponse",
    }


def test_committed_e2e_spec_matches_its_builder():
    """Synced to dfe-ui by the same workflow, so it goes stale the same way."""
    from dfe_engine.api.e2e_docs import build_e2e_spec

    # Built at the REPO's version, never at the file's own: handing the rebuild
    # the on-disk value makes the two sides agree by construction.
    on_disk = json.loads(E2E_SPEC_FILE.read_text())
    rebuilt = build_e2e_spec(version=VERSION_FILE.read_text().strip())

    assert _drop_version(on_disk) == _drop_version(rebuilt), (
        f"openapi.e2e.json is stale -- {REGENERATE}"
    )
