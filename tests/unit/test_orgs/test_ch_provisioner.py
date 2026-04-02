#  Project:      DFE Engine
#  File:         tests/unit/test_orgs/test_ch_provisioner.py
#  Purpose:      Tests for OrgChProvisioner
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
from dfe_engine.orgs.ch_provisioner import OrgChProvisioner


class TestSqlGeneration:
    def test_provision_dedicated_db_generates_correct_sql(self):
        provisioner = OrgChProvisioner(ch_client=None)
        stmts = provisioner.build_provision_sql("acme", "dfe_acme")
        sql = "\n".join(stmts)
        assert "CREATE DATABASE IF NOT EXISTS dfe_acme" in sql
        assert "CREATE USER IF NOT EXISTS dfe_org_acme" in sql
        assert "GRANT SELECT ON dfe_acme.*" in sql

    def test_deprovision_generates_correct_sql(self):
        provisioner = OrgChProvisioner(ch_client=None)
        stmts = provisioner.build_deprovision_sql("acme", "dfe_acme")
        sql = "\n".join(stmts)
        assert "DROP USER IF EXISTS dfe_org_acme" in sql
        assert "DROP DATABASE" not in sql


class TestNaming:
    def test_user_naming(self):
        provisioner = OrgChProvisioner(ch_client=None)
        assert provisioner.ch_user_name("acme") == "dfe_org_acme"
        assert provisioner.ch_user_name("my-org") == "dfe_org_my_org"

    def test_database_naming(self):
        provisioner = OrgChProvisioner(ch_client=None)
        assert provisioner.database_name("acme") == "dfe_acme"


class TestPasswordGeneration:
    def test_password_length(self):
        provisioner = OrgChProvisioner(ch_client=None)
        pw = provisioner.generate_password()
        assert len(pw) == 32

    def test_passwords_unique(self):
        provisioner = OrgChProvisioner(ch_client=None)
        assert provisioner.generate_password() != provisioner.generate_password()


class TestProvisionNonFatal:
    def test_provision_no_client_returns_false(self):
        provisioner = OrgChProvisioner(ch_client=None)
        success, password = provisioner.provision("acme")
        assert success is False
        assert password == ""

    def test_deprovision_no_client_returns_false(self):
        provisioner = OrgChProvisioner(ch_client=None)
        assert provisioner.deprovision("acme") is False
