#  Project:      dfe-engine
#  File:         tests/unit/test_governance/ch_reconciler_cases.py
#  Purpose:      Case tables for the CH RBAC reconciler's tenant fence
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the CH RBAC reconciler's tenant fence."""

from typing import TypedDict

# Tables the fence discovers carrying `_org_id`, as (database, table).
ORG_TABLES = [("dfe", "all_sources"), ("dfe", "main"), ("dfe", "zeek")]

# Tables in a tenant-granted database, `_org_id` or not, as (database, table).
REACHABLE_TABLES = [("dfe", "all_sources"), ("dfe", "hunt_state"), ("dfe", "main"), ("dfe", "zeek")]


class FenceTablesCase(TypedDict):
    id: str
    expected_applied: int
    expected_policies: list[str]
    tables: list[str] | None


FENCE_TABLES_CASES: list[FenceTablesCase] = [
    {
        "id": "one_org_table",
        "expected_applied": 7,
        "expected_policies": [
            "CREATE ROW POLICY IF NOT EXISTS `dfe_rowpol_tenant_dfe_all_sources` ON `dfe`.`all_sources` AS RESTRICTIVE FOR SELECT USING has(splitByChar(',', getSetting('SQL_current_tenant_id')), _org_id) TO `dfe_tenant_role`",
            "ALTER ROW POLICY `dfe_rowpol_tenant_dfe_all_sources` ON `dfe`.`all_sources` AS RESTRICTIVE FOR SELECT USING has(splitByChar(',', getSetting('SQL_current_tenant_id')), _org_id) TO `dfe_tenant_role`",
        ],
        "tables": ["all_sources"],
    },
    {
        "id": "one_table_without_org_id",
        "expected_applied": 7,
        "expected_policies": [
            "CREATE ROW POLICY IF NOT EXISTS `dfe_rowpol_tenant_dfe_hunt_state` ON `dfe`.`hunt_state` AS RESTRICTIVE FOR SELECT USING 0 TO `dfe_tenant_role`",
            "ALTER ROW POLICY `dfe_rowpol_tenant_dfe_hunt_state` ON `dfe`.`hunt_state` AS RESTRICTIVE FOR SELECT USING 0 TO `dfe_tenant_role`",
        ],
        "tables": ["hunt_state"],
    },
    {
        "id": "a_table_nothing_discovered",
        "expected_applied": 5,
        "expected_policies": [],
        "tables": ["absent"],
    },
    {
        "id": "every_table",
        "expected_applied": 13,
        "expected_policies": [
            "CREATE ROW POLICY IF NOT EXISTS `dfe_rowpol_tenant_dfe_all_sources` ON `dfe`.`all_sources` AS RESTRICTIVE FOR SELECT USING has(splitByChar(',', getSetting('SQL_current_tenant_id')), _org_id) TO `dfe_tenant_role`",
            "ALTER ROW POLICY `dfe_rowpol_tenant_dfe_all_sources` ON `dfe`.`all_sources` AS RESTRICTIVE FOR SELECT USING has(splitByChar(',', getSetting('SQL_current_tenant_id')), _org_id) TO `dfe_tenant_role`",
            "CREATE ROW POLICY IF NOT EXISTS `dfe_rowpol_tenant_dfe_main` ON `dfe`.`main` AS RESTRICTIVE FOR SELECT USING has(splitByChar(',', getSetting('SQL_current_tenant_id')), _org_id) TO `dfe_tenant_role`",
            "ALTER ROW POLICY `dfe_rowpol_tenant_dfe_main` ON `dfe`.`main` AS RESTRICTIVE FOR SELECT USING has(splitByChar(',', getSetting('SQL_current_tenant_id')), _org_id) TO `dfe_tenant_role`",
            "CREATE ROW POLICY IF NOT EXISTS `dfe_rowpol_tenant_dfe_zeek` ON `dfe`.`zeek` AS RESTRICTIVE FOR SELECT USING has(splitByChar(',', getSetting('SQL_current_tenant_id')), _org_id) TO `dfe_tenant_role`",
            "ALTER ROW POLICY `dfe_rowpol_tenant_dfe_zeek` ON `dfe`.`zeek` AS RESTRICTIVE FOR SELECT USING has(splitByChar(',', getSetting('SQL_current_tenant_id')), _org_id) TO `dfe_tenant_role`",
            "CREATE ROW POLICY IF NOT EXISTS `dfe_rowpol_tenant_dfe_hunt_state` ON `dfe`.`hunt_state` AS RESTRICTIVE FOR SELECT USING 0 TO `dfe_tenant_role`",
            "ALTER ROW POLICY `dfe_rowpol_tenant_dfe_hunt_state` ON `dfe`.`hunt_state` AS RESTRICTIVE FOR SELECT USING 0 TO `dfe_tenant_role`",
        ],
        "tables": None,
    },
]
