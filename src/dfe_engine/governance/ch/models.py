#  Project:      dfe-engine
#  File:         governance/ch/models.py
#  Purpose:      Config models for CH quota tiers, service roles, group bindings
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Config models for the ClickHouse RBAC machinery (gitcrud, versioned).

A TIER is "how much you can consume + what you can do" (grants + settings profile
+ quota). A SERVICE ROLE is a fixed identity (loader/query_reader/hunt_runner).

Tenant isolation uses the PRODUCTION-STANDARD custom-settings model (PostHog /
Grafana / LaunchDarkly), NOT a CH user per group or a row policy per (org, table):
a SMALL FIXED set of users by privilege (``ChFixedUser`` / ``FIXED_USERS``) plus
ONE row policy per ``_org_id`` table driven by the ``SQL_current_tenant_id``
custom setting (rendered by ``render_tenant_policies``). Adding the thousandth org
is zero DDL - the reader user and the per-table policy already exist; the engine
just injects that org's id into the per-query setting. This REPLACES the retired
per-org role + per-group user minting.

CH object naming (spec 5.1): a tier ``analyst_tier_2`` yields role
``dfe_analyst_tier_2_role``, profile ``dfe_analyst_tier_2_profile``, quota
``dfe_analyst_tier_2_quota``. The catalogue is config, never a hardcoded list.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_GiB = 1024**3

# ---- Fixed-user tenant-scoping model (custom settings) --------------------
#
# The custom-settings model's SSoT. The tenant axis is ONE row policy per
# ``_org_id`` table (render_tenant_policies), driven by a per-query custom
# setting, targeting the single row-filtered reader - not a policy per (org,
# table) and not a CH user per group.

# The ONE custom setting the tenant row policy reads. Uses ClickHouse's SQL_
# custom-settings prefix, which is PORTABLE across both deployment targets
# (live-proven 2026-07-06): on ClickHouse Cloud SQL_ is the BUILT-IN
# custom-setting prefix (nothing to configure); on self-hosted CH it must be
# allowed once via ``<custom_settings_prefixes>SQL_</custom_settings_prefixes>``
# in the server config - a DEPLOY prerequisite (dfe-infra CH chart / dfe-docker),
# NOT reconciler DDL. We use SQL_ (not a product-specific prefix) precisely
# because CH Cloud REJECTS a custom prefix like DFE_ and does not expose
# custom_settings_prefixes. Without the prefix on self-host, CH rejects both
# ``CREATE USER ... SETTINGS SQL_current_tenant_id`` and every per-query
# ``SETTINGS SQL_current_tenant_id``, so tenant scoping cannot apply. See
# docs/RBAC.md section 5 + .env.example.
TENANT_SETTING = "SQL_current_tenant_id"

# The single row-policy short-name reused on every ``_org_id`` table. A CH
# row-policy name is scoped per-table, so the same name on two tables is two
# distinct policies (one filter definition, applied everywhere).
TENANT_POLICY_NAME = "dfe_tenant_filter"

# The four fixed CH identities by privilege. ``dfe_admin`` is the deployment's CH
# superuser standing in as the admin data identity (the ``default`` connection +
# the reconciler's own admin_client); it is deliberately NOT minted, so the admin
# connection authenticates even before the first reconcile and the engine never
# auto-provisions a GRANT-ALL user. The other three ARE minted by the reconciler
# via the scalo.secrets seam (see FIXED_USERS).
ADMIN_USER = "dfe_admin"
ANALYST_USER = "dfe_analyst"
ANALYST_RO_USER = "dfe_analyst_ro"
TENANT_READER_USER = "dfe_tenant_reader"


# ---- Config models --------------------------------------------------------


class ChTier(BaseModel):
    """A quota tier: consumption envelope reconciled to profile + quota + role.

    Fully user-CRUD-able via governed-ops. The engine has NO hardcoded tier list;
    the reconciler applies whatever tiers exist in config.
    """

    name: str
    kind: str = "analyst"  # analyst | hunt (the two seeded families; extensible)
    default: bool = False  # the fallback tier FOR ITS KIND (one default per kind)
    grants: list[str] = Field(default_factory=list)  # analyst = SELECT; hunt adds INSERT
    settings: dict[str, int] = Field(default_factory=dict)  # -> CREATE SETTINGS PROFILE
    # quota -> CREATE QUOTA: an ``interval`` (str) plus per-interval maxima (ints).
    quota: dict[str, Any] = Field(default_factory=dict)

    def role(self) -> str:
        return f"dfe_{self.name}_role"

    def profile(self) -> str:
        return f"dfe_{self.name}_profile"

    def quota_name(self) -> str:
        return f"dfe_{self.name}_quota"

    def quota_interval(self) -> str:
        return str(self.quota.get("interval", "1 hour"))

    def quota_maxima(self) -> dict[str, int]:
        """The quota's per-interval maxima (everything except ``interval``)."""
        return {k: int(v) for k, v in self.quota.items() if k != "interval"}


class ChServiceRole(BaseModel):
    """A fixed service identity (loader / query_reader / hunt_runner base).

    Not tiered. ``mint_user`` creates a CH user + secret via the scalo.secrets
    seam; otherwise the role is granted to an existing identity by the reconciler.
    """

    name: str
    mint_user: bool = False
    grants: list[str] = Field(default_factory=list)
    settings: dict[str, int] = Field(default_factory=dict)

    def role(self) -> str:
        return f"dfe_{self.name}_role"

    def profile(self) -> str:
        return f"dfe_{self.name}_profile"

    def user(self) -> str:
        """The minted user name (used when ``mint_user`` is set)."""
        return f"dfe_{self.name}"


class ChFixedUser(BaseModel):
    """One of the small fixed set of CH users distinguished only by PRIVILEGE.

    Not per-org, not per-group. The tenant axis is ONE row policy per ``_org_id``
    table (render_tenant_policies) driven by the ``SQL_current_tenant_id`` custom
    setting, targeting only ``tenant_filtered`` users; every other fixed user is
    targeted by NO policy and therefore sees ALL rows (the CH restrictive-only
    property). Fields:

    - ``grants``   : the GRANTs applied straight to the user (no intermediate
      role - the proof grants directly; fewer objects, matches PostHog/Grafana).
    - ``readonly`` : set the CH ``readonly = 1`` profile bit (belt-and-braces on
      top of SELECT-only grants).
    - ``tenant_filtered`` : the row-filtered reader. Additionally makes
      ``SQL_current_tenant_id`` CHANGEABLE_IN_READONLY so it can be set per query
      while the user stays read-only, and is the user the tenant policy targets.
    """

    name: str
    grants: list[str] = Field(default_factory=list)
    readonly: bool = False
    tenant_filtered: bool = False


# ---- Seeded defaults (opinionated, non-destructive seeds) ------------------
#
# Three tiers per family. Operators edit/delete/extend these as ordinary
# governed-ops edits; seeding skips any file that already exists so edits are
# never clobbered. Memory + timeouts are the spec's locked numbers (section 4);
# quota maxima are reasonable starting points (per-interval caps), fully tunable.


def _analyst_tier(name: str, mem: int, secs: int, queries: int, *, default: bool = False) -> ChTier:
    return ChTier(
        name=name,
        kind="analyst",
        default=default,
        grants=["SELECT ON dfe.*", "SELECT ON dfe_hunts.*"],
        settings={
            "readonly": 1,
            "max_memory_usage": mem,
            "max_execution_time": secs,
            "max_rows_to_read": 0,  # 0 = unset
        },
        quota={
            "interval": "1 hour",
            "queries": queries,
            "result_rows": 1_000_000_000,
            "errors": 100,
        },
    )


def _hunt_tier(name: str, mem: int, secs: int, queries: int, *, default: bool = False) -> ChTier:
    return ChTier(
        name=name,
        kind="hunt",
        default=default,
        # Hunts read the data and write detections; no readonly.
        grants=["SELECT ON dfe.*", "SELECT ON dfe_hunts.*", "INSERT ON dfe_hunts.*"],
        settings={"max_memory_usage": mem, "max_execution_time": secs},
        quota={
            "interval": "1 hour",
            "queries": queries,
            "result_rows": 10_000_000_000,
            "errors": 500,
        },
    )


DEFAULT_TIERS: list[ChTier] = [
    _analyst_tier("analyst_tier_1", 16 * _GiB, 600, 5000),
    _analyst_tier("analyst_tier_2", 4 * _GiB, 300, 1000, default=True),
    _analyst_tier("analyst_tier_3", 1 * _GiB, 2, 200),
    _hunt_tier("hunt_tier_1", 16 * _GiB, 600, 10000),
    _hunt_tier("hunt_tier_2", 4 * _GiB, 120, 5000, default=True),
    _hunt_tier("hunt_tier_3", 1 * _GiB, 15, 1000),
]

# Fixed service identities (spec 5.3) - single, not tiered. Seeded like the
# tiers: non-destructive, operator-editable.
DEFAULT_SERVICE_ROLES: list[ChServiceRole] = [
    # dfe-loader: async-insert profile + INSERT on the data dbs.
    ChServiceRole(
        name="loader",
        mint_user=True,
        grants=["INSERT ON dfe.*", "INSERT ON dfe_hunts.*"],
        settings={
            "async_insert": 1,
            "wait_for_async_insert": 1,
            "wait_for_async_insert_timeout": 120,
            "async_insert_busy_timeout_max_ms": 1000,
            "async_insert_max_data_size": 134217728,
        },
    ),
    # The restricted engine reader - folds the old query/ddl.py dfe_query_reader
    # into ONE definition. readonly, no DDL, bounded per query.
    ChServiceRole(
        name="query_reader",
        mint_user=True,
        grants=["SELECT ON dfe.*", "SELECT ON dfe_hunts.*"],
        settings={
            "readonly": 1,
            "allow_ddl": 0,
            "max_execution_time": 30,
            "max_rows_to_read": 10_000_000,
            "max_memory_usage": 2 * _GiB,
        },
    ),
    # Hunt-runner coordination role: read/write on the hunt_lease/watermark/state
    # tables. Granted alongside a hunt tier at bind time; global (no org role), so
    # not a minted user of its own.
    ChServiceRole(
        name="hunt_runner",
        mint_user=False,
        grants=["SELECT ON dfe_hunts.*", "INSERT ON dfe_hunts.*"],
    ),
]

# The MINTED fixed users by privilege (the custom-settings tenant model). Three,
# not four: ``dfe_admin`` (ADMIN_USER) is the deployment's CH superuser / the
# ``default`` connection, never minted here (bootstrap-safe, no auto GRANT-ALL).
# The reconciler mints these via the secrets seam and grants them directly; the
# tenant row policy (render_tenant_policies) targets only dfe_tenant_reader.
FIXED_USERS: list[ChFixedUser] = [
    # data_analyst -> read + write the engine data DBs (no readonly).
    ChFixedUser(
        name=ANALYST_USER,
        grants=[
            "SELECT ON dfe.*",
            "SELECT ON dfe_hunts.*",
            "INSERT ON dfe.*",
            "INSERT ON dfe_hunts.*",
        ],
    ),
    # data_analyst_ro / data_viewer / infra_ro -> read-only the engine data DBs.
    ChFixedUser(
        name=ANALYST_RO_USER,
        grants=["SELECT ON dfe.*", "SELECT ON dfe_hunts.*"],
        readonly=True,
    ),
    # org_analyst -> read-only AND row-filtered to the caller's org(s) by the ONE
    # tenant row policy via SQL_current_tenant_id (empty setting = 0 rows).
    ChFixedUser(
        name=TENANT_READER_USER,
        grants=["SELECT ON dfe.*", "SELECT ON dfe_hunts.*"],
        readonly=True,
        tenant_filtered=True,
    ),
]
