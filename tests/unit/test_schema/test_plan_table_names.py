#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_plan_table_names.py
#  Purpose:      The names of the tables the core manifest owns in the data database, the landing table among them
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The tables the core manifest owns in the data database and the one it lands events in, read off the pinned dfe-schemas manifest."""

from dfe_engine.schema.core_schema import landing_table_name
from dfe_engine.schema.plan import data_table_names


class TestDataTableNames:
    def test_core_tables_in_the_data_database(self):
        names = data_table_names(data_database="dfe")

        assert {
            "core tables": {"detection", "main", "repository"} <= names,
            "meta database table": "orgs" in names,
            "view": "dfe_v_system_health" in names,
        } == {"core tables": True, "meta database table": False, "view": False}

    def test_a_renamed_data_database(self):
        assert "main" in data_table_names(data_database="dfe_prod")


class TestLandingTableName:
    def test_the_manifest_names_it(self):
        assert landing_table_name(data_database="dfe") == "main"
