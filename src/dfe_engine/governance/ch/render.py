#  Project:      dfe-engine
#  File:         governance/ch/render.py
#  Purpose:      Pure config -> ClickHouse DDL rendering (no cluster, unit-testable)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Pure rendering of the CH RBAC config into idempotent ClickHouse DDL.

Every function is ``config -> list[str]`` with no I/O, so the whole DDL surface is
unit-testable without a cluster (spec section 7). The reconciler executes these +
also writes them as gitops ``.sql`` artifacts so CH can be rebuilt from git.

Identifiers are backtick-quoted so names with hyphens (e.g. an org ``soc-ap``)
stay valid. All statements are idempotent (``IF NOT EXISTS``).
"""

from __future__ import annotations

from typing import Any

from .models import (
    FIXED_USERS,
    TENANT_POLICY_NAME,
    TENANT_READER_USER,
    TENANT_SETTING,
    ChFixedUser,
    ChServiceRole,
    ChTier,
)


def _bq(identifier: str) -> str:
    """Backtick-quote a CH identifier (escaping embedded backticks)."""
    return "`" + identifier.replace("`", "``") + "`"


def _sq(value: str) -> str:
    """Single-quote a CH string literal (escape backslashes THEN single quotes).

    ClickHouse honours C-style backslash escapes inside string literals, so the
    backslash MUST be doubled before the quote is doubled - otherwise a crafted
    value like ``\\' OR 1=1`` breaks out of a RESTRICTIVE row-policy predicate and
    tenant isolation fails open (F-ROWPOLICY-BACKSLASH, proven live on CH 25.8).
    Order is load-bearing: escape ``\\`` first so the ``''`` we emit for a quote is
    not itself re-escaped. Matches clickhouse-connect's escape_str.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _settings_kv(settings: dict[str, int]) -> str:
    """``k1 = v1, k2 = v2`` for a SETTINGS PROFILE, sorted for stable diffs."""
    return ", ".join(f"{k} = {v}" for k, v in sorted(settings.items()))


def render_tier(tier: ChTier) -> list[str]:
    """DDL for a quota tier: role -> settings profile -> quota -> grants -> attach.

    Ordering is load-bearing:
    - the ROLE is created FIRST because it is the grantee of ``CREATE QUOTA ... TO
      role`` and the target of ``ALTER ROLE ... SETTINGS PROFILE``; on a fresh
      cluster a QUOTA whose grantee role does not yet exist fails outright
      (F-RENDER-TIER-ORDER), and
    - the profile is created before the ALTER ROLE that attaches it.

    Profile + quota use ``OR REPLACE`` (not ``IF NOT EXISTS``): the reconciler's
    contract is to make ClickHouse MATCH the config, so an EDITED tier must actually
    re-apply on a cluster where the object already exists - ``IF NOT EXISTS`` would
    silently no-op the edit. Re-rendering an UNCHANGED tier is still idempotent
    (``OR REPLACE`` twice with the same body ends in the same state, no error).
    """
    role = _bq(tier.role())
    stmts: list[str] = [f"CREATE ROLE IF NOT EXISTS {role}"]

    if tier.settings:
        prof = _bq(tier.profile())
        stmts.append(
            f"CREATE SETTINGS PROFILE OR REPLACE {prof} SETTINGS {_settings_kv(tier.settings)}"
        )

    if tier.quota:
        qn = _bq(tier.quota_name())
        maxima = _settings_kv(tier.quota_maxima())
        stmts.append(
            f"CREATE QUOTA OR REPLACE {qn} "
            f"FOR INTERVAL {tier.quota_interval()} MAX {maxima} TO {role}"
        )

    for grant in tier.grants:
        stmts.append(f"GRANT {grant} TO {role}")
    if tier.settings:
        stmts.append(f"ALTER ROLE {role} SETTINGS PROFILE {_bq(tier.profile())}")
    return stmts


def render_service_role(role_def: ChServiceRole) -> list[str]:
    """DDL for a fixed service role (role + profile + grants + attach).

    Role first (grantee before the ALTER ROLE that attaches the profile), same
    ordering discipline as render_tier. The minted USER (when ``mint_user`` is set)
    is created by the reconciler, which holds the freshly minted secret - kept out
    of this pure path.
    """
    role = _bq(role_def.role())
    stmts: list[str] = [f"CREATE ROLE IF NOT EXISTS {role}"]

    if role_def.settings:
        prof = _bq(role_def.profile())
        # OR REPLACE (same reason as render_tier): an edited service-role profile
        # must re-apply on a cluster where it already exists, not silently no-op.
        stmts.append(
            f"CREATE SETTINGS PROFILE OR REPLACE {prof} SETTINGS {_settings_kv(role_def.settings)}"
        )

    for grant in role_def.grants:
        stmts.append(f"GRANT {grant} TO {role}")
    if role_def.settings:
        stmts.append(f"ALTER ROLE {role} SETTINGS PROFILE {_bq(role_def.profile())}")
    return stmts


def render_service_user(role_def: ChServiceRole, pw_hash: str) -> list[str]:
    """DDL for a minted service USER (``mint_user``): create + grant its role.

    Separate from ``render_service_role`` because it carries the freshly minted
    password hash; the reconciler calls it only after minting the secret.
    """
    qu = _bq(role_def.user())
    stmts = [
        f"CREATE USER IF NOT EXISTS {qu} IDENTIFIED WITH sha256_hash BY {_sq(pw_hash)}",
        f"GRANT {_bq(role_def.role())} TO {qu}",
    ]
    if role_def.settings:
        stmts.append(f"ALTER USER {qu} SETTINGS PROFILE {_bq(role_def.profile())}")
    return stmts


def _fixed_user_settings(fu: ChFixedUser) -> list[str]:
    """The ``SETTINGS`` bits for a fixed user (readonly + the tenant setting).

    Ordered/stable: ``readonly = 1`` then, for the row-filtered reader,
    ``DFE_current_tenant_id = '' CHANGEABLE_IN_READONLY``. The reader is readonly
    but MUST set the ONE tenant setting per query, so that single setting is
    changeable-in-readonly; its default is empty, which fails CLOSED (0 rows) until
    a real tenant id is injected. The setting name is escaped-safe (identifier, no
    quotes needed) and the empty default value goes through ``_sq``.
    """
    bits: list[str] = []
    if fu.readonly:
        bits.append("readonly = 1")
    if fu.tenant_filtered:
        bits.append(f"{TENANT_SETTING} = {_sq('')} CHANGEABLE_IN_READONLY")
    return bits


def render_fixed_users(hashes: dict[str, str]) -> list[str]:
    """DDL for the small fixed set of CH users by privilege (custom-settings model).

    Replaces the retired per-group minting: instead of one CH user per RBAC group,
    a handful of users differing only by PRIVILEGE. ``hashes`` maps a fixed user's
    name -> its sha256 password hash (minted by the reconciler via the scalo.secrets
    seam); a user with no hash is skipped (no secrets store -> no user), the same
    discipline as the service users. Grants are applied straight to the user.

    Idempotency: ``CREATE USER IF NOT EXISTS`` no-ops on an existing user and would
    therefore NEVER re-apply an edited SETTINGS clause, so a trailing
    ``ALTER USER ... SETTINGS`` re-applies readonly + the changeable tenant setting
    every run (safe to repeat).
    """
    stmts: list[str] = []
    for fu in FIXED_USERS:
        pw_hash = hashes.get(fu.name)
        if pw_hash is None:
            continue
        qu = _bq(fu.name)
        create = f"CREATE USER IF NOT EXISTS {qu} IDENTIFIED WITH sha256_hash BY {_sq(pw_hash)}"
        bits = _fixed_user_settings(fu)
        if bits:
            create += " SETTINGS " + ", ".join(bits)
        stmts.append(create)
        for grant in fu.grants:
            stmts.append(f"GRANT {grant} TO {qu}")
        if bits:
            stmts.append(f"ALTER USER {qu} SETTINGS " + ", ".join(bits))
    return stmts


def render_tenant_policies(tables: list[tuple[str, str]]) -> list[str]:
    """ONE RESTRICTIVE row policy per ``_org_id``-bearing table - the whole tenant
    axis, driven by the per-query ``DFE_current_tenant_id`` custom setting.

    ``tables`` is the ``(db, table)`` list the reconciler discovers from
    ``system.columns``. Every policy shares the short-name ``dfe_tenant_filter``
    (a CH row-policy name is per-table, so this is one filter definition applied
    everywhere) and targets ONLY ``dfe_tenant_reader``::

        USING has(splitByChar(',', getSetting('DFE_current_tenant_id')), _org_id)

    - a comma-joined tenant list ``'acme,globex'`` scopes the reader to those orgs
      (multi-org users), and
    - an EMPTY setting -> ``splitByChar(',', '')`` = ``['']`` -> a real ``_org_id``
      is never in it -> 0 rows: FAIL CLOSED. The engine must set the setting to
      ``''`` for an org-scoped principal with no orgs, never omit it.

    RESTRICTIVE-only is load-bearing (spec 5.2, proven live CH PR #34596): the
    un-targeted fixed users (admin/analyst/analyst_ro) are targeted by NO policy
    and see ALL rows; only the reader is filtered. NEVER emit PERMISSIVE - it would
    flip the table to default-deny for everyone. ``OR REPLACE`` (not
    ``IF NOT EXISTS``) so re-running re-applies the predicate onto an existing
    policy ("make ClickHouse match the config"); no counters to reset.
    """
    predicate = f"has(splitByChar(',', getSetting({_sq(TENANT_SETTING)})), _org_id)"
    target_role = _bq(TENANT_READER_USER)
    policy = _bq(TENANT_POLICY_NAME)
    stmts: list[str] = []
    for db, table in tables:
        target = f"{_bq(db)}.{_bq(table)}"
        stmts.append(
            f"CREATE ROW POLICY OR REPLACE {policy} ON {target} "
            f"AS RESTRICTIVE FOR SELECT USING {predicate} TO {target_role}"
        )
    return stmts


def render_materialise(orgs: list[Any], tiers: list[Any]) -> list[str]:
    """DDL+DML projecting the gitops SoT into read-only CH meta tables (spec 9).

    ``dfe_meta.orgs`` + ``dfe_meta.ch_tiers`` are ReplacingMergeTree projections;
    gitops stays the source of truth. Each reconcile TRUNCATEs + re-INSERTs so the
    projection exactly reflects current config (drops of removed orgs/tiers
    included) - idempotent and always current.
    """
    stmts: list[str] = [
        "CREATE DATABASE IF NOT EXISTS dfe_meta",
        (
            "CREATE TABLE IF NOT EXISTS dfe_meta.orgs "
            "(name String, org_ids Array(String), display_name String, "
            "enabled UInt8, updated_at DateTime DEFAULT now()) "
            "ENGINE = ReplacingMergeTree(updated_at) ORDER BY name"
        ),
        (
            "CREATE TABLE IF NOT EXISTS dfe_meta.ch_tiers "
            "(name String, kind String, is_default UInt8, "
            "updated_at DateTime DEFAULT now()) "
            "ENGINE = ReplacingMergeTree(updated_at) ORDER BY name"
        ),
        "TRUNCATE TABLE dfe_meta.orgs",
        "TRUNCATE TABLE dfe_meta.ch_tiers",
    ]
    for o in orgs:
        ids = ", ".join(_sq(i) for i in (o.org_ids or []))
        display = getattr(o, "display_name", "") or ""
        enabled = 1 if getattr(o, "enabled", True) else 0
        stmts.append(
            "INSERT INTO dfe_meta.orgs (name, org_ids, display_name, enabled) VALUES "
            f"({_sq(o.name)}, [{ids}], {_sq(display)}, {enabled})"
        )
    for t in tiers:
        stmts.append(
            "INSERT INTO dfe_meta.ch_tiers (name, kind, is_default) VALUES "
            f"({_sq(t.name)}, {_sq(t.kind)}, {1 if t.default else 0})"
        )
    return stmts
