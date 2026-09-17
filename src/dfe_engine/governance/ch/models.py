#  Project:      dfe-engine
#  File:         governance/ch/models.py
#  Purpose:      Config models for CH quota tiers, service roles, group bindings
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Config models for the ClickHouse RBAC machinery (gitcrud, versioned).

A TIER is "how much you can consume + what you can do" (grants + settings profile
+ quota). A SERVICE ROLE is a fixed identity (loader/query_reader/hunt_runner). A
GROUP BINDING ties an RBAC group's CH user to one tier axis + at most one org
axis. The tenant axis is one SHARED role plus a per-user pinned setting; per-org
users are derived from the Org registry by the reconciler.

The seed CATALOGUE -- the six tiers, the four service roles, the tenant axis, the
system-table grant lists and the naming rules -- is data in dfe-schemas
(``roles/clickhouse.yaml``) and is read from there. The models here are the shape
an operator's own governed-ops edits take.

CH object naming (spec 5.1): a tier ``analyst_tier_2`` yields role
``dfe_analyst_tier_2_role``, profile ``dfe_analyst_tier_2_profile``, quota
``dfe_analyst_tier_2_quota``, from the naming rules the catalogue declares.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from dfe_schemas import schemas_root
from dfe_schemas.loader import load_version_entry
from pydantic import BaseModel, Field

# ---- The seed catalogue, read from dfe-schemas ----------------------------

CATALOGUE_REF = "roles/clickhouse"


@lru_cache(maxsize=1)
def catalogue() -> dict[str, Any]:
    """The ClickHouse role, tier and grant catalogue, as dfe-schemas declares it."""
    return load_version_entry(schemas_root() / f"{CATALOGUE_REF}.yaml", require_columns=False)


def _naming(key: str) -> str:
    return str(catalogue()["naming"][key])


# ---- Data-scoping databases + grants (tenant isolation, spec 5.2) ----------
#
# Seed values an operator can edit. They name no database: see DB below.

DB = str(catalogue()["database_placeholder"])
"""Placeholder for THE DFE database, resolved at reconcile time.

Every table DFE writes lives in one database - the landing table, per-source
tables, hunt output, the OTel telemetry tables and the engine's own state.
Splitting them by purpose bought nothing: ClickHouse GRANT has no table-name
wildcard, so a split only multiplies db-wide grants, and isolation is by ROW
POLICY either way.

The NAME is never written here. This layer is pure config with no runtime
settings, so a literal would silently ignore a deployment that sets
``DFE_CLICKHOUSE_DATA_DATABASE`` and grant on a database it does not have. The
reconciler substitutes ``clickhouse.effective_data_database`` (see
``resolve_grant_databases``).
"""

# The analyst tier grants the WHOLE data database to EVERY user, org and platform
# alike: an org_viewer sees every source table with no admin action. Isolation is
# by ROW POLICY, not grant scope - a RESTRICTIVE `_org_id` policy fences each org
# to its own rows, and a `USING 0` deny policy hides the tables that carry no
# `_org_id` (otel_*, meta, the engine's own state). The reconciler DISCOVERS which
# tables those are, so a new one is fenced without anyone remembering.
# (D9: reverses D8's per-table grant narrowing.)
BROAD_DATA_GRANT = f"SELECT ON {DB}.*"


# ClickHouse's own introspection tables, granted to the platform reader so the
# pre-canned ClickHouse dashboards (and an operator writing ad-hoc SQL) can ask
# the server about itself. Named one by one rather than `system.*`: a tenant must
# never reach these, and an explicit list is what the rest of the model does.
#
# `merge('system', '^metric_log')` SKIPS a table the user cannot read, so a
# ClickHouse upgrade that rotates metric_log to metric_log_0 drops the older
# history out of the charts until that name is added to the catalogue.
CH_SYSTEM_TABLES = tuple(catalogue()["system_introspection_tables"])

SYSTEM_INTROSPECTION_GRANTS = [f"SELECT ON system.{table}" for table in CH_SYSTEM_TABLES]


# The only system tables a TENANT reaches. HyperDX builds its field list from
# system.columns and detects server capabilities from the other three, so a
# tenant that cannot read these renders an empty source rather than its data.
TENANT_SYSTEM_TABLES = tuple(catalogue()["tenant"]["system_tables"])

TENANT_SYSTEM_GRANTS = [f"SELECT ON system.{table}" for table in TENANT_SYSTEM_TABLES]


TENANT_ROLE = str(catalogue()["tenant"]["role"])
"""The ONE shared role the tenant row policies target.

Held by every org-pinned user and by nothing else. A user holding it reads only
the ``_org_id`` values named by its own pinned tenant setting; a user without it
is targeted by no policy and reads unrestricted.
"""

TENANT_SETTING = str(catalogue()["tenant"]["setting"])
"""Custom setting carrying a user's tenant ids (comma-joined), pinned READONLY.

The pin is the enforcement: a READONLY user setting rejects any override -
including a ``SETTINGS`` clause inside attacker-authored query text - with
SETTING_CONSTRAINT_VIOLATION (code 452). The server must allow the ``SQL_``
custom-settings prefix (the clickhouse-cluster chart does).
"""

# The governance projection's database and tables, by manifest id. The engine's
# schema phase creates them; the reconciler only writes rows into them.
META_DATABASE_ID = "db.meta"
META_ORGS_ID = "meta.orgs"
META_TIERS_ID = "meta.ch_tiers"


def meta_projection() -> tuple[str, str, str]:
    """``(database, orgs table, tiers table)`` as the manifest declares them."""
    from dfe_engine.schema.plan import object_names

    names = object_names(META_DATABASE_ID, META_ORGS_ID, META_TIERS_ID)
    return names[META_DATABASE_ID], names[META_ORGS_ID], names[META_TIERS_ID]


def tenant_policy_name(db: str, table: str) -> str:
    """Deterministic name for the shared tenant policy on one table."""
    return _naming("row_policy").format(database=db, table=table)


def org_user_name(org: str) -> str:
    """The org's pinned CH user - the identity a hyperdx team connects as."""
    return _naming("org_user").format(org=org)


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
        return _naming("role").format(name=self.name)

    def profile(self) -> str:
        return _naming("settings_profile").format(name=self.name)

    def quota_name(self) -> str:
        return _naming("quota").format(name=self.name)

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
        return _naming("role").format(name=self.name)

    def profile(self) -> str:
        return _naming("settings_profile").format(name=self.name)

    def user(self) -> str:
        """The minted user name (used when ``mint_user`` is set)."""
        return f"dfe_{self.name}"


class GroupChBinding(BaseModel):
    """An RBAC group's CH identity, composed from the two reusable role axes.

    Inline grants/settings/quota are GONE (they moved to tiers, spec 6.4): a
    binding is just a group -> (tier, org) pointer. Empty ``tier`` resolves to the
    default analyst tier; empty ``org`` means unrestricted (no tenant pin).
    """

    group: str
    ch_user: str = ""  # defaults to dfe_grp_{group}
    tier: str = ""  # -> dfe_{tier}_role (empty = the default analyst tier)
    org: str = ""  # -> tenant pin with that org's org_ids (empty = unrestricted)
    # Extra service-role names composed onto the tier (e.g. otel_reader);
    # quotas and settings still come from the tier alone.
    ch_roles: list[str] = Field(default_factory=list)

    def user(self) -> str:
        return self.ch_user or f"dfe_grp_{self.group}"


# ---- Seeded defaults, read from the dfe-schemas catalogue ------------------
#
# The tiers, their memory and timeout envelopes, their quotas and the service
# roles are DATA in ``roles/clickhouse.yaml``. Operators edit, delete and extend
# them as ordinary governed-ops edits; seeding skips any file that already
# exists, so an edit is never clobbered.


def _tier(entry: dict[str, Any]) -> ChTier:
    return ChTier(
        name=entry["name"],
        kind=entry.get("kind", "analyst"),
        default=bool(entry.get("default", False)),
        grants=list(entry.get("grants") or []),
        settings=dict(entry.get("settings") or {}),
        quota=dict(entry.get("quota") or {}),
    )


def _service_role(entry: dict[str, Any]) -> ChServiceRole:
    """One service role; ``system_introspection`` expands to the declared grant list."""
    grants = list(entry.get("grants") or [])
    if entry.get("system_introspection"):
        grants += SYSTEM_INTROSPECTION_GRANTS
    return ChServiceRole(
        name=entry["name"],
        mint_user=bool(entry.get("mint_user", False)),
        grants=grants,
        settings=dict(entry.get("settings") or {}),
    )


DEFAULT_TIERS: list[ChTier] = [_tier(entry) for entry in catalogue()["tiers"]]

# Fixed service identities (spec 5.3) - single, not tiered.
DEFAULT_SERVICE_ROLES: list[ChServiceRole] = [
    _service_role(entry) for entry in catalogue()["service_roles"]
]
