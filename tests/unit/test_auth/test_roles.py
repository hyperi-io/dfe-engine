#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_roles.py
#  Purpose:      Tests for RoleConfig with YAML roles and wildcard permission matching
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

import pytest

from dfe_engine.auth.roles import (
    ROLE_ALIASES,
    RoleConfig,
    RoleDefinition,
    permission_matches,
)

# ---------------------------------------------------------------------------
# _normalize_role_raw (resource-type alias) tests
# ---------------------------------------------------------------------------


class TestNormalizeResourceType:
    def test_hyphenated_resource_type_is_honoured(self):
        """FIX 5: a custom role authored with 'resource-type' (hyphen) keeps its type.

        The hyphen alias must fold onto the canonical 'resource_type'; before the
        fix the alias was ignored and the role defaulted to 'custom'.
        """
        data = {
            "roles": {
                "my_custom_role": {
                    "description": "Custom",
                    "permissions": ["source:read"],
                    "resource-type": "core",
                }
            }
        }
        config = RoleConfig._from_data(data)
        assert config.roles["my_custom_role"].resource_type == "core"

    def test_underscore_spelling_still_works(self):
        data = {
            "roles": {
                "my_custom_role": {
                    "description": "Custom",
                    "permissions": ["source:read"],
                    "resource_type": "core",
                }
            }
        }
        config = RoleConfig._from_data(data)
        assert config.roles["my_custom_role"].resource_type == "core"


# ---------------------------------------------------------------------------
# permission_matches tests
# ---------------------------------------------------------------------------


class TestPermissionMatches:
    def test_exact_match(self):
        assert permission_matches("config:read", "config:read") is True

    def test_exact_mismatch(self):
        assert permission_matches("config:read", "config:write") is False

    def test_wildcard_star_matches_everything(self):
        assert permission_matches("*", "config:read") is True
        assert permission_matches("*", "argo:applications:sync") is True
        assert permission_matches("*", "anything") is True

    def test_domain_wildcard_same_segment_count(self):
        # "config:*" matches "config:read" and "config:write"
        assert permission_matches("config:*", "config:read") is True
        assert permission_matches("config:*", "config:write") is True

    def test_domain_wildcard_does_not_match_different_domain(self):
        assert permission_matches("config:*", "source:read") is False

    def test_trailing_wildcard_argo_style(self):
        # "argo:*" has 2 segments; action "argo:applications:sync" has 3 segments
        # Last segment is *, so it's a trailing wildcard — should match
        assert permission_matches("argo:*", "argo:applications:sync") is True
        assert permission_matches("argo:*", "argo:applications:get") is True
        assert permission_matches("argo:*", "argo:projects:get") is True

    def test_trailing_wildcard_does_not_match_different_prefix(self):
        assert permission_matches("argo:*", "config:read") is False
        assert permission_matches("argo:*", "source:read") is False

    def test_service_scoped_wildcard_middle(self):
        # "service:*:config:read" matches "service:dfe-loader:config:read"
        assert permission_matches("service:*:config:read", "service:dfe-loader:config:read") is True
        assert (
            permission_matches("service:*:config:read", "service:dfe-receiver:config:read") is True
        )

    def test_service_scoped_wildcard_does_not_match_different_suffix(self):
        assert (
            permission_matches("service:*:config:read", "service:dfe-loader:config:write") is False
        )

    def test_service_scoped_wildcard_multi(self):
        # "service:*:config:*" matches any service, any config action
        assert permission_matches("service:*:config:*", "service:dfe-loader:config:read") is True
        assert permission_matches("service:*:config:*", "service:dfe-loader:config:write") is True

    def test_segment_count_mismatch_non_wildcard_last(self):
        # "config:read" (2 segments) vs "config:read:extra" (3 segments)
        # Last segment is NOT *, no trailing wildcard → False
        assert permission_matches("config:read", "config:read:extra") is False
        assert permission_matches("config:read", "a:config:read") is False

    def test_mid_wildcard_segment_count_must_match(self):
        # "service:*:config:read" (4 segments) vs "service:dfe-loader:config:read:extra" (5) → False
        # Last segment is NOT *, no trailing wildcard, segment count mismatch → False
        assert (
            permission_matches("service:*:config:read", "service:dfe-loader:config:read:extra")
            is False
        )

    def test_single_segment_exact(self):
        assert permission_matches("admin", "admin") is True
        assert permission_matches("admin", "viewer") is False

    def test_star_only_matches_all_segment_counts(self):
        # Bare "*" is special — matches regardless of segment count
        assert permission_matches("*", "a") is True
        assert permission_matches("*", "a:b") is True
        assert permission_matches("*", "a:b:c") is True

    def test_trailing_wildcard_multi_hop(self):
        # "hunt:*" with trailing wildcard should match "hunt:read", "hunt:write", etc.
        assert permission_matches("hunt:*", "hunt:read") is True
        assert permission_matches("hunt:*", "hunt:write") is True
        assert permission_matches("hunt:*", "hunt:execute:deep") is True


# ---------------------------------------------------------------------------
# RoleDefinition model tests
# ---------------------------------------------------------------------------


class TestRoleDefinition:
    def test_role_definition_defaults(self):
        rd = RoleDefinition(description="Test role", permissions=["*"])
        assert rd.description == "Test role"
        assert rd.permissions == ["*"]
        assert rd.scoped is False

    def test_role_definition_scoped(self):
        rd = RoleDefinition(description="Scoped role", permissions=["query:execute"], scoped=True)
        assert rd.scoped is True


# ---------------------------------------------------------------------------
# RoleConfig construction and has_permission tests
# ---------------------------------------------------------------------------


class TestRoleConfig:
    @pytest.fixture
    def sample_roles(self) -> dict[str, RoleDefinition]:
        return {
            "admin": RoleDefinition(description="Full access", permissions=["*"]),
            "data_analyst": RoleDefinition(
                description="Hunt and query",
                permissions=["hunt:*", "query:*", "source:read"],
            ),
            "infra": RoleDefinition(
                description="Infrastructure",
                permissions=["config:*", "argo:*", "service:*:config:*"],
            ),
            "viewer": RoleDefinition(
                description="Read-only",
                permissions=["config:read", "source:read"],
            ),
        }

    @pytest.fixture
    def config(self, sample_roles) -> RoleConfig:
        return RoleConfig(roles=sample_roles)

    def test_has_permission_admin_wildcard(self, config):
        assert config.has_permission("admin", "anything:at:all") is True

    def test_has_permission_exact_match(self, config):
        assert config.has_permission("viewer", "config:read") is True
        assert config.has_permission("viewer", "source:read") is True

    def test_has_permission_denied(self, config):
        assert config.has_permission("viewer", "config:write") is False
        assert config.has_permission("viewer", "hunt:read") is False

    def test_has_permission_domain_wildcard(self, config):
        assert config.has_permission("data_analyst", "hunt:read") is True
        assert config.has_permission("data_analyst", "hunt:write") is True
        assert config.has_permission("data_analyst", "query:execute") is True

    def test_has_permission_trailing_wildcard_argo(self, config):
        assert config.has_permission("infra", "argo:applications:sync") is True
        assert config.has_permission("infra", "argo:projects:get") is True

    def test_has_permission_service_scoped(self, config):
        assert config.has_permission("infra", "service:dfe-loader:config:read") is True
        assert config.has_permission("infra", "service:dfe-receiver:config:write") is True

    def test_has_permission_unknown_role(self, config):
        assert config.has_permission("nonexistent_role", "config:read") is False

    def test_has_permission_config_wildcard(self, config):
        assert config.has_permission("infra", "config:read") is True
        assert config.has_permission("infra", "config:write") is True


# ---------------------------------------------------------------------------
# RoleConfig.check_roles tests
# ---------------------------------------------------------------------------


class TestCheckRoles:
    @pytest.fixture
    def config(self) -> RoleConfig:
        return RoleConfig(
            roles={
                "admin": RoleDefinition(description="Admin", permissions=["*"]),
                "viewer": RoleDefinition(
                    description="Viewer",
                    permissions=["config:read", "source:read"],
                ),
                "operator": RoleDefinition(
                    description="Operator",
                    permissions=["config:read", "config:write", "source:read"],
                ),
            }
        )

    def test_check_roles_returns_first_granting_role(self, config):
        # viewer and operator both grant config:read — viewer comes first in the list
        result = config.check_roles(["viewer", "operator"], "config:read")
        assert result == "viewer"

    def test_check_roles_later_role_grants(self, config):
        # only operator grants config:write
        result = config.check_roles(["viewer", "operator"], "config:write")
        assert result == "operator"

    def test_check_roles_no_grant_returns_none(self, config):
        # neither viewer nor operator grant helm:compile
        result = config.check_roles(["viewer", "operator"], "helm:compile")
        assert result is None

    def test_check_roles_admin_grants_everything(self, config):
        result = config.check_roles(["admin"], "anything:at:all")
        assert result == "admin"

    def test_check_roles_empty_roles_returns_none(self, config):
        result = config.check_roles([], "config:read")
        assert result is None

    def test_check_roles_unknown_role_returns_none(self, config):
        result = config.check_roles(["nonexistent"], "config:read")
        assert result is None

    def test_check_roles_multi_role_first_match(self, config):
        # admin comes before viewer — admin should win
        result = config.check_roles(["admin", "viewer"], "config:read")
        assert result == "admin"


# ---------------------------------------------------------------------------
# RoleConfig.resolve_permissions tests
# ---------------------------------------------------------------------------


class TestResolvePermissions:
    @pytest.fixture
    def config(self) -> RoleConfig:
        return RoleConfig(
            roles={
                "viewer": RoleDefinition(
                    description="Viewer",
                    permissions=["config:read", "source:read"],
                ),
                "operator": RoleDefinition(
                    description="Operator",
                    permissions=["config:read", "config:write", "query:execute"],
                ),
                "data_analyst": RoleDefinition(
                    description="Analyst",
                    permissions=["hunt:*", "query:*"],
                ),
            }
        )

    def test_resolve_single_role(self, config):
        perms = config.resolve_permissions(["viewer"])
        assert perms == {"config:read", "source:read"}

    def test_resolve_multiple_roles_union(self, config):
        perms = config.resolve_permissions(["viewer", "operator"])
        assert perms == {"config:read", "source:read", "config:write", "query:execute"}

    def test_resolve_all_roles_union(self, config):
        perms = config.resolve_permissions(["viewer", "operator", "data_analyst"])
        assert "hunt:*" in perms
        assert "query:*" in perms
        assert "config:read" in perms

    def test_resolve_empty_roles_returns_empty_set(self, config):
        perms = config.resolve_permissions([])
        assert perms == set()

    def test_resolve_unknown_role_ignored(self, config):
        perms = config.resolve_permissions(["viewer", "nonexistent"])
        assert perms == {"config:read", "source:read"}

    def test_resolve_deduplicates_permissions(self, config):
        # viewer and operator both have config:read — should appear once in set
        perms = config.resolve_permissions(["viewer", "operator"])
        assert perms.count("config:read") if isinstance(perms, list) else "config:read" in perms


# ---------------------------------------------------------------------------
# RoleConfig.load tests (from YAML file)
# ---------------------------------------------------------------------------


class TestRoleConfigLoad:
    def test_load_from_yaml_file(self, tmp_path):
        yaml_content = """
roles:
  admin:
    description: "Full access"
    permissions:
      - "*"
  viewer:
    description: "Read only"
    permissions:
      - "config:read"
      - "source:read"
    scoped: false
  org_analyst:
    description: "Scoped viewer"
    permissions:
      - "query:execute"
    scoped: true
"""
        config_file = tmp_path / "roles.yaml"
        config_file.write_text(yaml_content)

        config = RoleConfig.load(config_file)

        assert "admin" in config.roles
        assert "viewer" in config.roles
        assert "org_analyst" in config.roles

    def test_load_parses_role_definitions(self, tmp_path):
        yaml_content = """
roles:
  analyst:
    description: "Data analyst"
    permissions:
      - "hunt:*"
      - "query:*"
"""
        config_file = tmp_path / "roles.yaml"
        config_file.write_text(yaml_content)

        config = RoleConfig.load(config_file)
        role = config.roles["analyst"]

        assert role.description == "Data analyst"
        assert "hunt:*" in role.permissions
        assert "query:*" in role.permissions
        assert role.scoped is False

    def test_load_parses_scoped_flag(self, tmp_path):
        yaml_content = """
roles:
  scoped_viewer:
    description: "Scoped"
    permissions:
      - "query:execute"
    scoped: true
"""
        config_file = tmp_path / "roles.yaml"
        config_file.write_text(yaml_content)

        config = RoleConfig.load(config_file)
        assert config.roles["scoped_viewer"].scoped is True

    def test_load_nonexistent_file_raises(self):
        with pytest.raises(FileNotFoundError):
            RoleConfig.load(Path("/nonexistent/roles.yaml"))

    def test_load_and_check_permissions(self, tmp_path):
        yaml_content = """
roles:
  infra:
    description: "Infrastructure admin"
    permissions:
      - "config:*"
      - "argo:*"
"""
        config_file = tmp_path / "roles.yaml"
        config_file.write_text(yaml_content)

        config = RoleConfig.load(config_file)
        assert config.has_permission("infra", "config:read") is True
        assert config.has_permission("infra", "argo:applications:sync") is True
        assert config.has_permission("infra", "source:read") is False


# ---------------------------------------------------------------------------
# RoleConfig.load_builtin tests
# ---------------------------------------------------------------------------


class TestLoadBuiltin:
    def test_load_builtin_succeeds(self):
        config = RoleConfig.load_builtin()
        assert config is not None

    def test_load_builtin_has_all_seven_roles(self):
        config = RoleConfig.load_builtin()
        expected_roles = {
            "admin",
            "data_analyst",
            "data_analyst_ro",
            "data_viewer",
            "infra",
            "infra_ro",
            "org_analyst",
        }
        assert set(config.roles.keys()) == expected_roles

    def test_builtin_admin_has_wildcard(self):
        config = RoleConfig.load_builtin()
        assert config.has_permission("admin", "anything") is True
        assert "*" in config.roles["admin"].permissions

    def test_builtin_infra_grants_argo(self):
        config = RoleConfig.load_builtin()
        assert config.has_permission("infra", "argo:applications:sync") is True
        assert config.has_permission("infra", "argo:projects:get") is True

    def test_builtin_infra_grants_hunt(self):
        """infra owns hunts (hunt:* added in the 2026-07 role model)."""
        config = RoleConfig.load_builtin()
        assert config.has_permission("infra", "hunt:read") is True
        assert config.has_permission("infra", "hunt:write") is True
        assert config.has_permission("infra", "hunt:execute") is True

    def test_builtin_infra_grants_service_config(self):
        config = RoleConfig.load_builtin()
        assert config.has_permission("infra", "service:dfe-loader:config:read") is True
        assert config.has_permission("infra", "service:dfe-loader:config:write") is True

    def test_builtin_sigma_grants_reach_analyst_roles(self):
        # P2: sigma was admin-only by accident - data_analyst authors sigma, ro reads.
        config = RoleConfig.load_builtin()
        assert config.has_permission("data_analyst", "sigma:write") is True
        assert config.has_permission("data_analyst", "sigma:read") is True
        assert config.has_permission("data_analyst_ro", "sigma:read") is True
        assert config.has_permission("data_analyst_ro", "sigma:write") is False

    def test_builtin_infra_grants_lifecycle_and_bare_service_read(self):
        # P2: lifecycle:* was operator-grantable per docs but held by no role;
        # infra also lacked bare service:read (weaker infra_ro had it).
        config = RoleConfig.load_builtin()
        assert config.has_permission("infra", "lifecycle:read") is True
        assert config.has_permission("infra", "lifecycle:app:start") is True
        assert config.has_permission("infra", "service:read") is True
        assert config.has_permission("infra_ro", "lifecycle:read") is True
        assert config.has_permission("infra_ro", "lifecycle:app:start") is False

    def test_builtin_data_analyst_grants_cel_check(self):
        # P3: cel:check was an admin-only accident; the analyst authors expressions.
        config = RoleConfig.load_builtin()
        assert config.has_permission("data_analyst", "cel:check") is True

    def test_builtin_infra_ro_read_only_service(self):
        config = RoleConfig.load_builtin()
        assert config.has_permission("infra_ro", "service:dfe-loader:config:read") is True
        assert config.has_permission("infra_ro", "service:dfe-loader:config:write") is False

    def test_builtin_org_analyst_is_scoped(self):
        config = RoleConfig.load_builtin()
        assert config.roles["org_analyst"].scoped is True

    def test_builtin_data_analyst_grants_hunt(self):
        config = RoleConfig.load_builtin()
        assert config.has_permission("data_analyst", "hunt:read") is True
        assert config.has_permission("data_analyst", "hunt:write") is True
        assert config.has_permission("data_analyst", "schema:write") is True
        assert config.has_permission("data_analyst", "schema:delete") is False

    def test_builtin_data_analyst_ro_read_only(self):
        config = RoleConfig.load_builtin()
        assert config.has_permission("data_analyst_ro", "hunt:read") is True
        assert config.has_permission("data_analyst_ro", "hunt:write") is False
        assert config.has_permission("data_analyst_ro", "schema:write") is False
        assert config.has_permission("data_analyst_ro", "schema:delete") is False

    def test_builtin_data_viewer_limited(self):
        config = RoleConfig.load_builtin()
        assert config.has_permission("data_viewer", "query:execute") is True
        assert config.has_permission("data_viewer", "hunt:read") is False


# ---------------------------------------------------------------------------
# HyperDX capability block (config-driven) + cumulative resolver
# ---------------------------------------------------------------------------


class TestHyperdxAccessParsing:
    def test_builtin_roles_declare_hyperdx(self):
        config = RoleConfig.load_builtin()
        assert config.hyperdx_for("admin").access == "full"
        assert config.hyperdx_for("infra").access == "otel"
        assert config.hyperdx_for("infra_ro").access == "otel"
        assert config.hyperdx_for("data_analyst").access == "full"
        assert config.hyperdx_for("data_analyst_ro").access == "full"
        assert config.hyperdx_for("data_viewer").access == "full"

    def test_org_analyst_is_tenant_scoped(self):
        config = RoleConfig.load_builtin()
        hdx = config.hyperdx_for("org_analyst")
        assert hdx.access == "org-scoped"
        assert hdx.tenant_scoped is True

    def test_role_without_block_returns_none(self):
        config = RoleConfig(
            roles={"plain": RoleDefinition(description="No hdx", permissions=["source:read"])}
        )
        assert config.hyperdx_for("plain") is None

    def test_hyperdx_parsed_from_yaml(self, tmp_path):
        yaml_content = """
roles:
  ops:
    description: "Ops"
    permissions:
      - "config:read"
    hyperdx:
      access: otel
      tenant_scoped: false
"""
        config_file = tmp_path / "roles.yaml"
        config_file.write_text(yaml_content)
        config = RoleConfig.load(config_file)
        assert config.roles["ops"].hyperdx.access == "otel"
        assert config.roles["ops"].hyperdx.tenant_scoped is False


class TestEffectiveHyperdx:
    def test_no_roles_is_none(self):
        config = RoleConfig.load_builtin()
        eff = config.effective_hyperdx([])
        assert eff.access == "none"
        assert eff.tenant_scoped is False

    def test_widest_access_wins(self):
        config = RoleConfig.load_builtin()
        # org_analyst (org-scoped) + data_viewer (full) -> full wins.
        eff = config.effective_hyperdx(["org_analyst", "data_viewer"])
        assert eff.access == "full"
        assert eff.tenant_scoped is False

    def test_otel_beats_org_scoped(self):
        config = RoleConfig.load_builtin()
        eff = config.effective_hyperdx(["org_analyst", "infra"])
        assert eff.access == "otel"
        assert eff.tenant_scoped is False

    def test_all_org_scoped_stays_tenant_scoped(self):
        config = RoleConfig.load_builtin()
        # Only org-scoped roles -> tenant_scoped holds (per-org bindings).
        eff = config.effective_hyperdx(["org_analyst"])
        assert eff.access == "org-scoped"
        assert eff.tenant_scoped is True

    def test_roles_without_hyperdx_ignored(self):
        config = RoleConfig(
            roles={
                "plain": RoleDefinition(description="No hdx", permissions=["source:read"]),
            }
        )
        eff = config.effective_hyperdx(["plain"])
        assert eff.access == "none"


# ---------------------------------------------------------------------------
# ROLE_ALIASES back-compat shim (old role name -> new)
# ---------------------------------------------------------------------------


class TestRoleAliases:
    def test_alias_map_shape(self):
        assert ROLE_ALIASES == {
            "infra_admin": "infra",
            "infra_viewer": "infra_ro",
            "data_analyst_viewer": "data_analyst_ro",
            "customer_viewer": "org_analyst",
        }

    def test_old_infra_admin_resolves_infra_grants(self):
        """A group still referencing 'infra_admin' resolves the 'infra' grants."""
        config = RoleConfig.load_builtin()
        # argo:* is an existing infra grant; hunt:* is the newly-added one.
        assert config.has_permission("infra_admin", "argo:applications:sync") is True
        assert config.has_permission("infra_admin", "hunt:read") is True

    def test_old_names_resolve_via_resolve_permissions(self):
        config = RoleConfig.load_builtin()
        perms = config.resolve_permissions(["customer_viewer"])
        assert "query:execute" in perms

    def test_old_name_resolves_hyperdx(self):
        config = RoleConfig.load_builtin()
        hdx = config.hyperdx_for("customer_viewer")
        assert hdx is not None
        assert hdx.access == "org-scoped"

    def test_literal_name_wins_over_alias(self):
        """A config that literally (re)defines an old name keeps its own grants."""
        config = RoleConfig(
            roles={
                "infra_admin": RoleDefinition(
                    description="Legacy literal",
                    permissions=["source:read"],
                ),
            }
        )
        # Literal wins: resolves the custom role, not the aliased 'infra'.
        assert config.has_permission("infra_admin", "source:read") is True
        assert config.has_permission("infra_admin", "argo:applications:sync") is False
