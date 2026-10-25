#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_scoped_role_read_only.py
#  Purpose:      A scoped role grants reads and query:execute only, wherever it is loaded
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A ``scoped`` role bound at system scope passes every org-scope check, so it holds no writes.

The refusal applies to the shipped roles file, to a deployment's roles file and to
the role store's writes, and it names the role and every pattern it may not hold.
"""

import pytest

from dfe_engine.auth.role_store import RoleStore
from dfe_engine.auth.roles import RoleConfig

WRITES = ["org:write", "group:write", "repository:write"]


def _roles_file(tmp_path, permissions: list[str], *, scoped: bool = True):
    path = tmp_path / "roles.yaml"
    listed = "\n".join(f'      - "{permission}"' for permission in permissions)
    path.write_text(
        "roles:\n  tenant_admin:\n    description: a tenant admin\n"
        f"    permissions:\n{listed}\n    scoped: {'true' if scoped else 'false'}\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("write", WRITES)
def test_a_scoped_role_granting_a_write_is_refused_at_load(tmp_path, write):
    path = _roles_file(tmp_path, ["org:read", write])

    with pytest.raises(ValueError, match=f"Scoped role 'tenant_admin' .* not {write}$"):
        RoleConfig.load(path)


def test_the_refusal_names_every_pattern_it_may_not_hold():
    from dfe_engine.auth.roles import scoped_role_refusal

    message = scoped_role_refusal("tenant_admin", ["org:read", "group:*", "org:write", "*"])

    assert message == (
        "Scoped role 'tenant_admin' may grant only read actions and query:execute, "
        "not group:*, org:write, *"
    )


def test_the_same_writes_unscoped_load(tmp_path):
    config = RoleConfig.load(_roles_file(tmp_path, WRITES, scoped=False))

    assert config.roles["tenant_admin"].permissions == WRITES


@pytest.mark.parametrize(
    "permissions",
    [["query:execute", "source:read"], ["service:*:metrics:read"], ["*:read"]],
)
def test_reads_and_query_execute_load_scoped(tmp_path, permissions):
    config = RoleConfig.load(_roles_file(tmp_path, permissions))

    assert config.roles["tenant_admin"].scoped is True


def test_the_shipped_org_viewer_still_loads():
    from dfe_engine.auth.roles import scoped_role_refusal

    org_viewer = RoleConfig.load_builtin().roles["org_viewer"]

    assert org_viewer.scoped is True
    assert scoped_role_refusal("org_viewer", org_viewer.permissions) is None
    assert len(org_viewer.permissions) == 5


@pytest.mark.parametrize("write", WRITES)
def test_the_role_store_writes_no_scoped_role_granting_a_write(tmp_path, write):
    path = _roles_file(tmp_path, ["org:read"], scoped=False)
    before = path.read_text(encoding="utf-8")
    store = RoleStore(path)

    with pytest.raises(ValueError, match="Scoped role 'tenant_admin'"):
        store.update("tenant_admin", permissions=["org:read", write], scoped=True)
    with pytest.raises(ValueError, match="Scoped role 'other'"):
        store.create("other", description="x", permissions=[write], scoped=True)

    assert path.read_text(encoding="utf-8") == before
