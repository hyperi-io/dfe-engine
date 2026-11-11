#  Project:      dfe-engine
#  File:         tests/unit/test_governance/test_profile_parity.py
#  Purpose:      The schema phase and the reconciler write the same settings profiles
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Both paths ALTER the same profile, and an ALTER replaces its whole settings list.

So any difference in rendering means whichever runs last decides the profile,
constraints included.
"""

import pytest
from dfe_schemas import schemas_root
from dfe_schemas.clickhouse import Platform
from dfe_schemas.manifest import load_manifest
from dfe_schemas.render import Renderer

from dfe_engine.governance.ch.bootstrap import service_roles_for
from dfe_engine.governance.ch.models import DEFAULT_TIERS
from dfe_engine.governance.ch.render import render_service_role, render_tier
from dfe_engine.settings import DFESettings


def _profile_statement(statements: list[str]) -> str:
    (statement,) = [s for s in statements if s.startswith("ALTER SETTINGS PROFILE")]
    return statement


def _schema_phase_profiles() -> dict[str, str]:
    manifest = load_manifest(root=schemas_root())
    renderer = Renderer(manifest, platform=Platform.SELF_HOSTED)
    profiles = {}
    for obj in manifest.objects:
        if obj.kind != "role" or obj.section == "tenant":
            continue
        statements = list(renderer.render(obj).statements)
        if any(s.startswith("ALTER SETTINGS PROFILE") for s in statements):
            profiles[obj.key] = _profile_statement(statements)
    return profiles


def _reconciler_profiles() -> dict[str, str]:
    profiles = {t.name: _profile_statement(render_tier(t)) for t in DEFAULT_TIERS if t.settings}
    for role in service_roles_for(DFESettings(env="dev")):
        if role.settings:
            profiles[role.name] = _profile_statement(render_service_role(role))
    return profiles


SCHEMA_PHASE = _schema_phase_profiles()
RECONCILER = _reconciler_profiles()


def test_both_paths_write_the_same_profiles():
    assert set(SCHEMA_PHASE) == set(RECONCILER)


@pytest.mark.parametrize("name", sorted(SCHEMA_PHASE))
def test_a_profile_renders_identically_on_both_paths(name):
    assert RECONCILER[name] == SCHEMA_PHASE[name]
