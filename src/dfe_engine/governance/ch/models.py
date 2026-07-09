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
axis. Org roles are NOT modelled here - they are derived from the Org registry by
the reconciler (one role + row policy set per Org).

CH object naming (spec 5.1): a tier ``analyst_tier_2`` yields role
``dfe_analyst_tier_2_role``, profile ``dfe_analyst_tier_2_profile``, quota
``dfe_analyst_tier_2_quota``. The catalogue is config, never a hardcoded list.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---- CH object naming (deterministic, so drops are computable) ------------

_GiB = 1024**3


def org_role_name(org: str) -> str:
    """CH role that carries an org's restrictive row policies."""
    return f"dfe_org_{org}_role"


def org_policy_name(org: str, db: str, table: str) -> str:
    """Deterministic row-policy name for (org, table) - lets the reconciler diff."""
    return f"dfe_rowpol_{org}_{db}_{table}"


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


class GroupChBinding(BaseModel):
    """An RBAC group's CH identity, composed from the two reusable role axes.

    Inline grants/settings/quota are GONE (they moved to tiers, spec 6.4): a
    binding is just a group -> (tier, org) pointer. Empty ``tier`` resolves to the
    default analyst tier; empty ``org`` means unrestricted (no org role granted).
    """

    group: str
    ch_user: str = ""  # defaults to dfe_grp_{group}
    tier: str = ""  # -> dfe_{tier}_role (empty = the default analyst tier)
    org: str = ""  # -> dfe_org_{org}_role (empty = unrestricted)

    def user(self) -> str:
        return self.ch_user or f"dfe_grp_{self.group}"


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
