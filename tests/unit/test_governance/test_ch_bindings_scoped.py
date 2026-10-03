#  Project:      dfe-engine
#  File:         tests/unit/test_governance/test_ch_bindings_scoped.py
#  Purpose:      A scoped role's group gets its org's pinned ClickHouse user
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The CH group bindings read a role's ``scoped`` flag from the role definitions.

A system group holding only scoped roles is a tenant group: its ClickHouse user
is pinned to its org's tenant ids rather than reading every org. A role the
definitions do not declare grants nothing, so it unfences nothing either.
"""

from types import SimpleNamespace

from dfe_engine.auth.roles import RoleConfig, RoleDefinition
from dfe_engine.governance.ch.bindings import derive_group_bindings
from dfe_engine.governance.ch.bootstrap import reconcile_from_stores
from dfe_engine.governance.ch.models import TENANT_SETTING, group_user_name
from dfe_engine.settings import SecretsSettings

TENANT = RoleDefinition(description="a tenant's viewer", permissions=["query:execute"], scoped=True)


def _definitions(**roles: RoleDefinition) -> RoleConfig:
    return RoleConfig({**RoleConfig.load_builtin().roles, **roles})


def _org(name: str, ids: list[str]) -> SimpleNamespace:
    return SimpleNamespace(name=name, org_ids=ids)


def _group(name: str, roles: list[str], org_ids: list[str]) -> SimpleNamespace:
    return SimpleNamespace(name=name, scope_org="", org_ids=org_ids, roles=roles)


class _AdminClient:
    """Records executed DDL; every discovery query returns no rows."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def query(self, sql: str, parameters: dict | None = None) -> SimpleNamespace:
        return SimpleNamespace(result_rows=[])

    def command(self, stmt: str) -> None:
        self.executed.append(stmt)


def _orgs(bindings) -> dict[str, str]:
    return {binding.group: binding.org for binding in bindings}


def test_a_system_group_holding_a_scoped_role_is_pinned_to_its_org():
    bindings = derive_group_bindings(
        [_group("tenants", ["tenant_viewer"], ["acme"])],
        [_org("acme", ["t-acme"])],
        role_config=_definitions(tenant_viewer=TENANT),
    )

    assert _orgs(bindings) == {"tenants": "acme"}


def test_the_same_role_unscoped_reads_every_org():
    unscoped = TENANT.model_copy(update={"scoped": False})
    bindings = derive_group_bindings(
        [_group("platform", ["tenant_viewer"], ["acme"])],
        [_org("acme", ["t-acme"])],
        role_config=_definitions(tenant_viewer=unscoped),
    )

    assert _orgs(bindings) == {"platform": ""}


def test_a_scoped_role_beside_org_viewer_keeps_the_pin():
    bindings = derive_group_bindings(
        [_group("tenants", ["org_viewer", "tenant_viewer"], ["acme"])],
        [_org("acme", ["t-acme"])],
        role_config=_definitions(tenant_viewer=TENANT),
    )

    assert _orgs(bindings) == {"tenants": "acme"}


def test_a_platform_role_beside_a_scoped_one_still_reads_every_org():
    bindings = derive_group_bindings(
        [_group("mixed", ["tenant_viewer", "data_analyst"], ["acme"])],
        [_org("acme", ["t-acme"])],
        role_config=_definitions(tenant_viewer=TENANT),
    )

    assert _orgs(bindings) == {"mixed": ""}


def test_a_role_the_definitions_do_not_declare_does_not_unfence_the_group():
    bindings = derive_group_bindings(
        [_group("typo", ["data_anlayst"], ["acme"])],
        [_org("acme", ["t-acme"])],
        role_config=RoleConfig.load_builtin(),
    )

    assert _orgs(bindings) == {"typo": "acme"}


def test_the_reconcile_pins_a_scoped_groups_user_to_its_orgs_tenant_ids(tmp_path):
    client = _AdminClient()
    group = _group("tenants", ["tenant_viewer"], ["acme"])

    reconcile_from_stores(
        client,
        settings=SimpleNamespace(secrets=SecretsSettings(provider="file", path=str(tmp_path))),
        org_registry=SimpleNamespace(list=lambda: [_org("acme", ["t-acme-1", "t-acme-2"])]),
        group_store=SimpleNamespace(list=lambda: [group]),
        role_config=_definitions(tenant_viewer=TENANT),
    )

    user = group_user_name("tenants")
    pins = [stmt for stmt in client.executed if user in stmt and TENANT_SETTING in stmt]
    assert len(pins) == 1
    assert pins[0].endswith("= 't-acme-1,t-acme-2' READONLY")
