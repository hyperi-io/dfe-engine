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

from dfe_engine.clickhouse.quoting import quote_identifier, quote_literal

from .models import ChServiceRole, ChTier, org_policy_name, org_role_name


def _bq(identifier: str) -> str:
    """Backtick-quote a CH identifier - delegates to the canonical quoting seam."""
    return quote_identifier(identifier)


def _sq(value: str) -> str:
    """Single-quote a CH string literal - delegates to the canonical quoting seam.

    Uses ``quote_literal`` so backslashes are escaped BEFORE single quotes: a
    RESTRICTIVE row-policy predicate must not fail open via a crafted ``\\' OR 1=1``
    (F-ROWPOLICY-BACKSLASH). The old inline ``_sq`` doubled quotes only.
    """
    return quote_literal(value)


def _settings_kv(settings: dict[str, int]) -> str:
    """``k1 = v1, k2 = v2`` for a SETTINGS PROFILE, sorted for stable diffs."""
    return ", ".join(f"{k} = {v}" for k, v in sorted(settings.items()))


def render_tier(tier: ChTier) -> list[str]:
    """DDL for a quota tier: role -> settings profile -> quota -> grants -> attach.

    Order matters (spec 7 step 1). The role comes FIRST because the quota is
    assigned ``TO`` it, and ClickHouse rejects a quota naming a role that does not
    exist yet (UNKNOWN_ROLE) - which only shows up against a cluster where the
    role was not already present. The profile and quota in turn precede the
    ``ALTER ROLE`` that attaches them. Every statement is idempotent.
    """
    role = _bq(tier.role())
    stmts: list[str] = [f"CREATE ROLE IF NOT EXISTS {role}"]

    if tier.settings:
        prof = _bq(tier.profile())
        stmts.append(
            f"CREATE SETTINGS PROFILE IF NOT EXISTS {prof} SETTINGS {_settings_kv(tier.settings)}"
        )

    if tier.quota:
        qn = _bq(tier.quota_name())
        maxima = _settings_kv(tier.quota_maxima())
        stmts.append(
            f"CREATE QUOTA IF NOT EXISTS {qn} "
            f"FOR INTERVAL {tier.quota_interval()} MAX {maxima} TO {role}"
        )

    for grant in tier.grants:
        stmts.append(f"GRANT {grant} TO {role}")
    if tier.settings:
        stmts.append(f"ALTER ROLE {role} SETTINGS PROFILE {_bq(tier.profile())}")
    return stmts


def render_service_role(role_def: ChServiceRole) -> list[str]:
    """DDL for a fixed service role (profile + role + grants + attach).

    The minted USER (when ``mint_user`` is set) is created by the reconciler,
    which holds the freshly minted secret - kept out of this pure path.
    """
    role = _bq(role_def.role())
    stmts: list[str] = []

    if role_def.settings:
        prof = _bq(role_def.profile())
        stmts.append(
            f"CREATE SETTINGS PROFILE IF NOT EXISTS {prof} SETTINGS {_settings_kv(role_def.settings)}"
        )

    stmts.append(f"CREATE ROLE IF NOT EXISTS {role}")
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


def _org_predicate(org_ids: list[str]) -> str:
    """``_org_id = 'x'`` for a single id, ``_org_id IN ('a', 'b')`` for many."""
    if len(org_ids) == 1:
        return f"_org_id = {_sq(org_ids[0])}"
    return f"_org_id IN ({', '.join(_sq(i) for i in org_ids)})"


def render_org_role(org: str, org_ids: list[str], tables: list[tuple[str, str]]) -> list[str]:
    """DDL for an org's visibility axis: a role + one RESTRICTIVE row policy per
    ``_org_id``-bearing table.

    ``tables`` is the list of ``(db, table)`` that actually carry ``_org_id`` (the
    reconciler discovers these from ``system.columns``). RESTRICTIVE-only is
    load-bearing (spec 5.2): a user holding no org role is targeted by no policy
    and therefore sees ALL rows; a user holding this role sees only matching rows.
    NEVER emit a PERMISSIVE policy here - it would flip the table to default-deny.
    """
    role = _bq(org_role_name(org))
    stmts: list[str] = [f"CREATE ROLE IF NOT EXISTS {role}"]
    predicate = _org_predicate(org_ids)
    for db, table in tables:
        policy = _bq(org_policy_name(org, db, table))
        target = f"{_bq(db)}.{_bq(table)}"
        stmts.append(
            f"CREATE ROW POLICY IF NOT EXISTS {policy} ON {target} "
            f"AS RESTRICTIVE FOR SELECT USING {predicate} TO {role}"
        )
    return stmts


def render_group_user(
    user: str, pw_hash: str, *, tier_role: str, org_role: str | None = None
) -> list[str]:
    """DDL for a group's CH user: create it, then grant the tier + (optional) org
    role.

    ``tier_role`` / ``org_role`` are the RESOLVED CH role names (the reconciler
    resolves an empty tier to the default and an empty org to unrestricted). An
    unrestricted user simply gets no org grant.
    """
    qu = _bq(user)
    stmts = [
        f"CREATE USER IF NOT EXISTS {qu} IDENTIFIED WITH sha256_hash BY {_sq(pw_hash)}",
        f"GRANT {_bq(tier_role)} TO {qu}",
    ]
    if org_role:
        stmts.append(f"GRANT {_bq(org_role)} TO {qu}")
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
