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
stay valid. All statements are convergent: ``IF NOT EXISTS`` creates, and an
``ALTER`` re-asserts mutable state (settings, quotas, pins) so edits reach
objects that already exist.
"""

from __future__ import annotations

from typing import Any

from dfe_engine.clickhouse.quoting import quote_identifier, quote_literal

from .models import (
    TENANT_ROLE,
    TENANT_SETTING,
    TENANT_SYSTEM_GRANTS,
    ChServiceRole,
    ChTier,
    meta_projection,
    tenant_policy_name,
)


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
        kv = _settings_kv(tier.settings)
        # Create-then-alter so edits to an existing profile converge; a bare
        # IF NOT EXISTS silently ignores changed settings forever.
        stmts.append(f"CREATE SETTINGS PROFILE IF NOT EXISTS {prof}")
        stmts.append(f"ALTER SETTINGS PROFILE {prof} SETTINGS {kv}")

    if tier.quota:
        qn = _bq(tier.quota_name())
        maxima = _settings_kv(tier.quota_maxima())
        stmts.append(f"CREATE QUOTA IF NOT EXISTS {qn} TO {role}")
        stmts.append(
            f"ALTER QUOTA {qn} FOR INTERVAL {tier.quota_interval()} MAX {maxima} TO {role}"
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
        stmts.append(f"CREATE SETTINGS PROFILE IF NOT EXISTS {prof}")
        stmts.append(f"ALTER SETTINGS PROFILE {prof} SETTINGS {_settings_kv(role_def.settings)}")

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

    The ALTER re-asserts the password every reconcile. ``CREATE USER IF NOT
    EXISTS`` sets a password only when it FIRST creates the user, so an already
    existing user keeps whatever hash it was born with - and the served secret
    (``ch/service/<name>``) is the source of truth, so any drift between the two
    is a hard CH auth failure (code 516) until the user is realigned. The ALTER
    converges the user to the stored secret's hash on every run; it is a no-op
    when they already match.
    """
    qu = _bq(role_def.user())
    stmts = [
        f"CREATE USER IF NOT EXISTS {qu} IDENTIFIED WITH sha256_hash BY {_sq(pw_hash)}",
        f"ALTER USER {qu} IDENTIFIED WITH sha256_hash BY {_sq(pw_hash)}",
        f"GRANT {_bq(role_def.role())} TO {qu}",
    ]
    if role_def.settings:
        stmts.append(f"ALTER USER {qu} SETTINGS PROFILE {_bq(role_def.profile())}")
    return stmts


def render_tenant_axis(
    tables: list[tuple[str, str]],
    deny_tables: list[tuple[str, str]] | None = None,
) -> list[str]:
    """DDL for the SHARED tenant axis: one role, its system grants, one RESTRICTIVE
    policy per table.

    The predicate reads the caller's pinned ``SQL_current_tenant_id`` (comma-joined
    tenant ids), so ONE policy set serves every org - adding an org adds a pinned
    user and nothing here. ``tables`` is the list of ``(db, table)`` that actually
    carry ``_org_id`` (discovered from ``system.columns``).

    ``deny_tables`` are tenant-reachable tables that carry NO ``_org_id``. The
    row policies only cover ``_org_id`` tables, but the analyst tier grants
    ``SELECT ON <db>.*``, so an un-annotated table in a granted db is read in full
    by every org (the hunt orchestration tables are this class). Each gets a
    RESTRICTIVE ``USING 0`` policy so a tenant reads nothing from it.

    RESTRICTIVE-only is load-bearing (spec 5.2): a user not holding
    ``dfe_tenant_role`` is targeted by no policy and reads ALL rows; a holder reads
    only its pinned ids. NEVER emit a PERMISSIVE policy here - it would flip the
    table to default-deny. ``getSetting`` on a holder with NO pin throws, so a
    misconfigured grant fails closed and loudly rather than leaking.
    """
    role = _bq(TENANT_ROLE)
    predicate = f"has(splitByChar(',', getSetting('{TENANT_SETTING}')), _org_id)"
    stmts: list[str] = [f"CREATE ROLE IF NOT EXISTS {role}"]
    # Under select_from_system_db_requires_grant the system database is
    # deny-by-default, so the embedded HyperDX cannot build a field list without
    # these four; the server config alone would leave a tenant staring at an
    # empty source.
    stmts += [f"GRANT {grant} TO {role}" for grant in TENANT_SYSTEM_GRANTS]
    for db, table in tables:
        stmts += _tenant_policy(db, table, using=predicate, role=role)
    for db, table in deny_tables or []:
        stmts += _tenant_policy(db, table, using="0", role=role)
    return stmts


def _tenant_policy(db: str, table: str, *, using: str, role: str) -> list[str]:
    """Create one table's tenant policy, then re-assert its filter in place.

    Both kinds share the one name, so a table that gains or loses ``_org_id``
    already holds a policy under it and ``IF NOT EXISTS`` alone would keep the old
    filter forever. The ALTER changes the filter in one step, so the table is
    never left with no restrictive policy at all.
    """
    policy = _bq(tenant_policy_name(db, table))
    target = f"{_bq(db)}.{_bq(table)}"
    body = f"AS RESTRICTIVE FOR SELECT USING {using} TO {role}"
    return [
        f"CREATE ROW POLICY IF NOT EXISTS {policy} ON {target} {body}",
        f"ALTER ROW POLICY {policy} ON {target} {body}",
    ]


def render_pinned_user(
    user: str,
    pw_hash: str,
    *,
    tier_role: str,
    org_ids: list[str],
    extra_roles: list[str] | None = None,
) -> list[str]:
    """DDL for a tenant-scoped (or unrestricted) CH user.

    With ``org_ids``: grant the shared tenant role and PIN the tenant setting
    READONLY - the pin is what makes an attacker-authored ``SETTINGS`` override a
    hard 452 instead of a cross-tenant read. The analyst tier role grants the
    whole data db ``dfe.*``, so the fenced org user sees every source table
    automatically; isolation is by the RESTRICTIVE ``_org_id`` row policy plus the
    ``USING 0`` deny policy on the non-``_org_id`` tables (dfe.otel_*, meta), NOT
    by grant scope.

    Empty ``org_ids`` is the unrestricted shape (PLATFORM analysts/operators):
    tier only, no tenant role, no pin. ``extra_roles`` compose additional grant
    roles (e.g. otel_reader) onto the tier; quotas and settings still come from
    the tier alone.

    Two ALTERs re-run every reconcile, because ``CREATE USER IF NOT EXISTS``
    never touches an existing user: the pin ALTER tracks org_ids changes, and
    the IDENTIFIED ALTER re-asserts the password. The served secret
    (``ch/orgs/<org>`` for the pinned org user) is the single source of truth,
    so a user that was created in a different epoch than its stored secret keeps
    a stale hash and every hyperdx connect fails CH auth (code 516) until it is
    realigned. The IDENTIFIED ALTER converges the user to the stored hash on
    every run; it is a no-op when they already match.
    """
    qu = _bq(user)
    stmts = [
        f"CREATE USER IF NOT EXISTS {qu} IDENTIFIED WITH sha256_hash BY {_sq(pw_hash)}",
        f"ALTER USER {qu} IDENTIFIED WITH sha256_hash BY {_sq(pw_hash)}",
        f"GRANT {_bq(tier_role)} TO {qu}",
    ]
    for role in extra_roles or []:
        stmts.append(f"GRANT {_bq(role)} TO {qu}")
    if org_ids:
        # The pin is comma-joined, so a comma inside an id would silently split
        # into fragments that match nothing.
        bad = [i for i in org_ids if "," in i]
        if bad:
            raise ValueError(f"org ids must not contain commas: {bad}")
        stmts.append(f"GRANT {_bq(TENANT_ROLE)} TO {qu}")
        pin = _sq(",".join(org_ids))
        stmts.append(f"ALTER USER {qu} SETTINGS {TENANT_SETTING} = {pin} READONLY")
    return stmts


def render_materialise(orgs: list[Any], tiers: list[Any]) -> list[str]:
    """DML projecting the gitops SoT into the read-only CH meta tables (spec 9).

    The two tables are declared in dfe-schemas and created by the engine's schema
    phase, which is the only path that issues DDL. What is left here is the
    PROJECTION: each reconcile TRUNCATEs and re-INSERTs so it exactly reflects
    current config, removed orgs and tiers included.

    The literal CREATEs that used to open this list pinned
    ``ReplacingMergeTree(updated)`` with no ``ON CLUSTER``, so on a cluster the
    tables landed on whichever replica the connection reached.
    """
    database, orgs_name, tiers_name = meta_projection()
    orgs_table = f"{database}.{orgs_name}"
    tiers_table = f"{database}.{tiers_name}"
    stmts: list[str] = [f"TRUNCATE TABLE {orgs_table}", f"TRUNCATE TABLE {tiers_table}"]
    for o in orgs:
        ids = ", ".join(_sq(i) for i in (o.org_ids or []))
        display = getattr(o, "display_name", "") or ""
        enabled = 1 if getattr(o, "enabled", True) else 0
        stmts.append(
            f"INSERT INTO {orgs_table} (name, org_ids, display_name, enabled) VALUES "
            f"({_sq(o.name)}, [{ids}], {_sq(display)}, {enabled})"
        )
    for t in tiers:
        stmts.append(
            f"INSERT INTO {tiers_table} (name, kind, is_default) VALUES "
            f"({_sq(t.name)}, {_sq(t.kind)}, {1 if t.default else 0})"
        )
    return stmts
