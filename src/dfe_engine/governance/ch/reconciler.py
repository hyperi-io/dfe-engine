#  Project:      dfe-engine
#  File:         governance/ch/reconciler.py
#  Purpose:      Reconcile the gitops CH-RBAC config into real ClickHouse objects
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ChRbacReconciler - read the gitops SoT, make ClickHouse match, idempotently.

Rendering is pure (``render_all``); ``reconcile`` adds the I/O: discover the
``_org_id``-bearing tables, mint-or-reuse the per-identity secrets, execute the
DDL, and drop objects for orgs that no longer exist. Every statement is
idempotent (``IF NOT EXISTS`` / computed diff for drops), so a second run is a
no-op. See docs/superpowers/specs/2026-07-01-ch-org-rbac-quota-tiers-design.md.
"""

from __future__ import annotations

import hashlib
import secrets as _stdlib_secrets
from typing import Any

from pydantic import BaseModel, Field
from scalo.logger import logger

from .models import DEFAULT_SERVICE_ROLES, DEFAULT_TIERS, org_user_name, tenant_policy_name
from .render import (
    _bq,
    render_materialise,
    render_pinned_user,
    render_service_role,
    render_service_user,
    render_tenant_axis,
    render_tier,
)

# Databases whose columns we never scan for _org_id or reconcile against.
_SKIP_DBS = ["system", "information_schema", "INFORMATION_SCHEMA"]


class ReconcileResult(BaseModel):
    """What a reconcile run did."""

    statements: list[str] = Field(default_factory=list)  # DDL applied
    dropped: list[str] = Field(default_factory=list)  # stale objects dropped
    minted: list[str] = Field(default_factory=list)  # identities whose secret was touched
    errors: list[str] = Field(default_factory=list)


def _default_tier_name(tiers: list[Any], kind: str) -> str:
    """Name of the default tier for a kind (the flagged default, else the first)."""
    of_kind = [t for t in tiers if t.kind == kind]
    for t in of_kind:
        if t.default:
            return t.name
    return of_kind[0].name if of_kind else ""


def compute_drops(
    existing_org_roles: set[str],
    existing_policies: list[tuple[str, str, str]],
    existing_org_users: set[str],
    orgs: list[Any],
    org_tables: list[tuple[str, str]],
) -> list[str]:
    """Pure diff: DROP DDL for tenant objects with no config behind them.

    Three sweeps, each prefix-scoped so hand-made CH objects are safe:

    - ``dfe_rowpol_*`` policies not in the desired tenant set. This also retires
      the pre-pinned design's per-org literal policies on upgrade.
    - ``dfe_org_*`` roles - ALL of them: the pinned design has no per-org roles,
      so any survivor is the old design's leftover.
    - ``dfe_org_*`` users for orgs no longer registered. An offboarded org whose
      credential stays live is still a tenant of the platform; the drop is the
      revocation.

    ``existing_policies`` is ``(short_name, db, table)`` from system.row_policies.
    """
    desired_policies = {tenant_policy_name(db, t) for (db, t) in org_tables}
    desired_users = {org_user_name(o.name) for o in orgs}
    drops: list[str] = []
    for short_name, db, table in existing_policies:
        if short_name.startswith("dfe_rowpol_") and short_name not in desired_policies:
            drops.append(f"DROP ROW POLICY IF EXISTS {_bq(short_name)} ON {_bq(db)}.{_bq(table)}")
    for role in sorted(existing_org_roles):
        if role.startswith("dfe_org_") and role.endswith("_role"):
            drops.append(f"DROP ROLE IF EXISTS {_bq(role)}")
    for user in sorted(existing_org_users):
        if user.startswith("dfe_org_") and user not in desired_users:
            drops.append(f"DROP USER IF EXISTS {_bq(user)}")
    return drops


class ChRbacReconciler:
    """Reconcile the gitops CH-RBAC config into real ClickHouse objects.

    ``admin_client`` is a clickhouse-connect client with admin rights (duck-typed:
    ``.command(stmt)`` + ``.query(sql, parameters=...).result_rows``).
    ``secrets_store`` is the scalo.secrets seam (``DfeSecrets``): when absent, tiers
    / roles / org policies still reconcile but no group/service USERS are minted.
    """

    def __init__(self, admin_client: Any, *, secrets_store: Any = None) -> None:
        self._client = admin_client
        self._secrets = secrets_store

    # ---- discovery -------------------------------------------------------

    def discover_org_id_tables(self) -> list[tuple[str, str]]:
        """(db, table) for every table carrying an ``_org_id`` column."""
        rows = self._client.query(
            "SELECT database, table FROM system.columns "
            "WHERE name = '_org_id' AND database NOT IN {skip:Array(String)} "
            "GROUP BY database, table ORDER BY database, table",
            parameters={"skip": _SKIP_DBS},
        ).result_rows
        return [(r[0], r[1]) for r in rows]

    def _existing_org_roles(self) -> set[str]:
        rows = self._client.query(
            "SELECT name FROM system.roles WHERE name LIKE 'dfe_org_%'"
        ).result_rows
        return {r[0] for r in rows}

    def _existing_org_users(self) -> set[str]:
        rows = self._client.query(
            "SELECT name FROM system.users WHERE name LIKE 'dfe_org_%'"
        ).result_rows
        return {r[0] for r in rows}

    def _existing_row_policies(self) -> list[tuple[str, str, str]]:
        rows = self._client.query(
            "SELECT short_name, database, table FROM system.row_policies "
            "WHERE short_name LIKE 'dfe_rowpol_%'"
        ).result_rows
        return [(r[0], r[1], r[2]) for r in rows]

    # ---- secret mint-or-reuse -------------------------------------------

    def _hash_for(self, path: str) -> str:
        """sha256 of the secret at ``path`` - reuse if present, else mint + store.

        Reuse is essential: ``CREATE USER IF NOT EXISTS`` never rotates an existing
        user's password, so regenerating every run would desync CH from the store.
        """
        if self._secrets is not None and self._secrets.exists(path):
            plaintext = self._secrets.get(path)
        else:
            plaintext = _stdlib_secrets.token_urlsafe(24)[:32]
            if self._secrets is not None:
                self._secrets.put(path, plaintext)
        return hashlib.sha256(plaintext.encode()).hexdigest()

    # ---- pure render -----------------------------------------------------

    def render_all(
        self,
        *,
        tiers: list[Any],
        service_roles: list[Any],
        orgs: list[Any],
        bindings: list[Any],
        org_tables: list[tuple[str, str]],
        service_hashes: dict[str, str],
        group_hashes: dict[str, str],
        org_hashes: dict[str, str],
    ) -> list[str]:
        """Full ordered DDL (spec 7): tiers -> service roles -> tenant axis ->
        org users -> group users. Pure given the discovered ``org_tables`` and the
        minted ``*_hashes``.
        """
        stmts: list[str] = []
        for t in tiers:
            stmts += render_tier(t)
        for r in service_roles:
            stmts += render_service_role(r)
            if r.mint_user and r.name in service_hashes:
                stmts += render_service_user(r, service_hashes[r.name])
        default_analyst = _default_tier_name(tiers, "analyst")
        default_tier_role = f"dfe_{default_analyst}_role"
        stmts += render_tenant_axis(org_tables)
        # The org's pinned user: the identity its hyperdx team connects as.
        orgs_by_name = {o.name: o for o in orgs}
        for o in orgs:
            if o.name not in org_hashes:
                continue  # no minted secret (no secrets store) -> skip the user
            stmts += render_pinned_user(
                org_user_name(o.name),
                org_hashes[o.name],
                tier_role=default_tier_role,
                org_ids=list(o.org_ids) or [o.name],
            )
        for b in bindings:
            if b.group not in group_hashes:
                continue
            tier_role = f"dfe_{b.tier or default_analyst}_role"
            org = orgs_by_name.get(b.org) if b.org else None
            org_ids = (list(org.org_ids) or [org.name]) if org is not None else []
            stmts += render_pinned_user(
                b.user(), group_hashes[b.group], tier_role=tier_role, org_ids=org_ids
            )
        return stmts

    # ---- reconcile -------------------------------------------------------

    def reconcile(
        self,
        *,
        tiers: list[Any],
        service_roles: list[Any],
        orgs: list[Any],
        bindings: list[Any],
    ) -> ReconcileResult:
        """Discover, mint, render, apply, and drop-stale, idempotently."""
        result = ReconcileResult()
        org_tables = self.discover_org_id_tables()

        service_hashes: dict[str, str] = {}
        group_hashes: dict[str, str] = {}
        org_hashes: dict[str, str] = {}
        if self._secrets is not None:
            for r in service_roles:
                if r.mint_user:
                    service_hashes[r.name] = self._hash_for(f"ch/service/{r.name}")
                    result.minted.append(f"service/{r.name}")
            for o in orgs:
                org_hashes[o.name] = self._hash_for(f"ch/orgs/{o.name}")
                result.minted.append(f"org/{o.name}")
            for b in bindings:
                group_hashes[b.group] = self._hash_for(f"ch/groups/{b.group}")
                result.minted.append(f"group/{b.group}")

        stmts = self.render_all(
            tiers=tiers,
            service_roles=service_roles,
            orgs=orgs,
            bindings=bindings,
            org_tables=org_tables,
            service_hashes=service_hashes,
            group_hashes=group_hashes,
            org_hashes=org_hashes,
        )
        drops = compute_drops(
            self._existing_org_roles(),
            self._existing_row_policies(),
            self._existing_org_users(),
            orgs,
            org_tables,
        )
        materialise = render_materialise(orgs, tiers)

        for stmt in stmts + drops + materialise:
            try:
                self._client.command(stmt)
            except Exception as exc:  # a bad statement must not abort the rest
                result.errors.append(f"{stmt[:60]}...: {exc}")
        result.statements = stmts + materialise
        result.dropped = drops

        if result.errors:
            logger.warning(
                "CH RBAC reconcile completed with errors", error_count=len(result.errors)
            )
        else:
            logger.info(
                "CH RBAC reconciled",
                statements=len(stmts),
                dropped=len(drops),
                minted=len(result.minted),
            )
        return result


def reconcile_ch_rbac(
    admin_client: Any,
    *,
    secrets_store: Any = None,
    orgs: list[Any] | None = None,
    tiers: list[Any] | None = None,
    service_roles: list[Any] | None = None,
    bindings: list[Any] | None = None,
) -> ReconcileResult:
    """The one entry point to reconcile the CH-RBAC config into ClickHouse.

    Defaults to the SEEDED tiers + service roles; pass explicit lists to override
    (e.g. once tiers load from gitcrud). ``bindings`` come from
    ``derive_group_bindings`` over the RBAC group store. ``secrets_store`` (the
    scalo.secrets seam) enables minting the service/group USERS - without it only
    tiers, roles and org row policies reconcile. Used by app.py startup and the
    governance API endpoint.
    """
    return ChRbacReconciler(admin_client, secrets_store=secrets_store).reconcile(
        tiers=tiers if tiers is not None else DEFAULT_TIERS,
        service_roles=service_roles if service_roles is not None else DEFAULT_SERVICE_ROLES,
        orgs=orgs or [],
        bindings=bindings or [],
    )
