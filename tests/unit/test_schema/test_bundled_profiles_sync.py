"""Guard: bundled fallback profiles must match the dfe-schemas submodule.

The bundled copy (``src/dfe_engine/schema/profiles/``) is the CI-generated
fallback used when the engine is pip-installed without the ``schemas``
submodule. It must never be hand-drifted from canonical ``common-header/``.

Regenerate with::

    cp schemas/common-header/*.yaml src/dfe_engine/schema/profiles/
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_BUNDLED = _ROOT / "src" / "dfe_engine" / "schema" / "profiles"
_CANONICAL = _ROOT / "schemas" / "common-header"
_PROFILES = ("timeseries", "minimal", "passthrough")


@pytest.mark.parametrize("name", _PROFILES)
def test_bundled_profile_matches_canonical(name: str) -> None:
    """Bundled profile YAML must be byte-identical to canonical common-header."""
    canonical = _CANONICAL / f"{name}.yaml"
    if not canonical.exists():
        pytest.skip("dfe-schemas submodule not checked out")

    bundled = _BUNDLED / f"{name}.yaml"
    assert bundled.read_text(encoding="utf-8") == canonical.read_text(encoding="utf-8"), (
        f"Bundled profile '{name}' drifted from schemas/common-header/. "
        f"Regenerate: cp schemas/common-header/{name}.yaml src/dfe_engine/schema/profiles/"
    )
