"""Guard: bundled fallback profiles must match the dfe-schemas package.

The bundled copy (``src/dfe_engine/schema/profiles/``) is the last-resort
fallback, used when neither ``DFE_SCHEMAS_DIR`` nor the ``dfe-schemas`` package
resolves. It must never be hand-drifted from canonical ``common-header/``.

Regenerate with::

    python -c "import dfe_schemas, shutil, pathlib; \
        [shutil.copy2(p, 'src/dfe_engine/schema/profiles/') \
         for p in (dfe_schemas.schemas_root() / 'common-header').glob('*.yaml')]"
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dfe_engine.schema.schema_loader import _resolve_package_schemas_root

_ROOT = Path(__file__).resolve().parents[3]
_BUNDLED = _ROOT / "src" / "dfe_engine" / "schema" / "profiles"
_PACKAGE_ROOT = _resolve_package_schemas_root()
_PROFILES = ("timeseries", "minimal", "passthrough")


@pytest.mark.parametrize("name", _PROFILES)
def test_bundled_profile_matches_canonical(name: str) -> None:
    """Bundled profile YAML must be byte-identical to the package's common-header."""
    if _PACKAGE_ROOT is None:
        pytest.skip("dfe-schemas package not installed")

    canonical = _PACKAGE_ROOT / "common-header" / f"{name}.yaml"
    if not canonical.exists():
        pytest.skip(f"dfe-schemas package carries no common-header/{name}.yaml")

    bundled = _BUNDLED / f"{name}.yaml"
    assert bundled.read_text(encoding="utf-8") == canonical.read_text(encoding="utf-8"), (
        f"Bundled profile '{name}' drifted from the dfe-schemas package. "
        f"Copy {canonical} over {bundled}."
    )
