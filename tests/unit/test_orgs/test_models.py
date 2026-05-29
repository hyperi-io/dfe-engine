#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_models.py
#  Purpose:      Tests for Org model validation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from dfe_engine.orgs.models import Org


class TestOrgModel:
    def test_minimal_org(self):
        org = Org(name="acme")
        assert org.name == "acme"
        assert org.display_name == ""
        assert org.org_ids == []
        assert org.enabled is True
        assert org.created_at == ""
        assert org.updated_at == ""

    def test_full_org(self):
        org = Org(
            name="acme",
            display_name="Acme Corp",
            org_ids=["acme", "acme-sub"],
            enabled=False,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-02T00:00:00+00:00",
        )
        assert org.name == "acme"
        assert org.display_name == "Acme Corp"
        assert org.org_ids == ["acme", "acme-sub"]
        assert org.enabled is False
        assert org.created_at == "2026-01-01T00:00:00+00:00"

    def test_org_ids_default_factory(self):
        """Each Org gets its own list, not a shared reference."""
        org1 = Org(name="one")
        org2 = Org(name="two")
        org1.org_ids.append("x")
        assert org2.org_ids == []

    def test_model_dump_includes_all_fields(self):
        org = Org(name="acme", org_ids=["acme"])
        data = org.model_dump()
        assert "name" in data
        assert "display_name" in data
        assert "org_ids" in data
        assert "enabled" in data

    def test_model_validate_from_dict(self):
        data = {
            "name": "acme",
            "display_name": "Acme Corp",
            "org_ids": ["acme"],
            "enabled": True,
        }
        org = Org.model_validate(data)
        assert org.name == "acme"
        assert org.display_name == "Acme Corp"

    def test_org_dedicated_database_default_false(self):
        org = Org(name="test")
        assert org.dedicated_database is False
        assert org.database_name == ""
        assert org.hyperdx_team_id == ""
        assert org.ch_password_env == ""

    def test_org_dedicated_database_enabled(self):
        org = Org(name="acme", dedicated_database=True, database_name="dfe_acme")
        assert org.dedicated_database is True
        assert org.database_name == "dfe_acme"

    def test_org_internal_metadata_roundtrip(self):
        org = Org(
            name="acme",
            dedicated_database=True,
            database_name="dfe_acme",
            hyperdx_team_id="team-abc",
            ch_password_env="DFE_ACME_CH_PASSWORD",
        )
        data = org.model_dump()
        assert data["dedicated_database"] is True
        assert data["database_name"] == "dfe_acme"
        assert data["hyperdx_team_id"] == "team-abc"
        assert data["ch_password_env"] == "DFE_ACME_CH_PASSWORD"
        restored = Org.model_validate(data)
        assert restored.hyperdx_team_id == "team-abc"
        assert restored.ch_password_env == "DFE_ACME_CH_PASSWORD"
