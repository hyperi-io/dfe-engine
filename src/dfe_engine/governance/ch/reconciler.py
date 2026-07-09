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

from .models import DEFAULT_SERVICE_ROLES, DEFAULT_TIERS, org_policy_name, org_role_name
from .render import (
    _bq,
    render_group_user,
    render_materialise,
    render_org_role,
    render_service_role,
    render_service_user,
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
    orgs: list[Any],
    org_tables: list[tuple[str, str]],
) -> list[str]:
    """Pure diff: DROP DDL for org roles/policies with no config Org behind them.

    Policies are dropped before roles (a policy targets a role). Only touches
    ``dfe_org_`` / ``dfe_rowpol_`` objects, so hand-made CH objects are safe.
    ``existing_policies`` is ``(short_name, db, table)`` from system.row_policies.
    """
    desired_roles = {org_role_name(o.name) for o in orgs}
    desired_policies = {org_policy_name(o.name, db, t) for o in orgs for (db, t) in org_tables}
    drops: list[str] = []
    for short_name, db, table in existing_policies:
        if short_name.startswith("dfe_rowpol_") and short_name not in desired_policies:
            drops.append(f"DROP ROW POLICY IF EXISTS {_bq(short_name)} ON {_bq(db)}.{_bq(table)}")
    for role in sorted(existing_org_roles):
        if role.startswith("dfe_org_") and role not in desired_roles:
            drops.append(f"DROP ROLE IF EXISTS {_bq(role)}")
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
    ) -> list[str]:
        """Full ordered DDL (spec 7): tiers -> service roles -> org roles -> group
        users. Pure given the discovered ``org_tables`` and the minted ``*_hashes``.
        """
        stmts: list[str] = []
        for t in tiers:
            stmts += render_tier(t)
        for r in service_roles:
            stmts += render_service_role(r)
            if r.mint_user and r.name in service_hashes:
                stmts += render_service_user(r, service_hashes[r.name])
        for o in orgs:
            stmts += render_org_role(o.name, list(o.org_ids) or [o.name], org_tables)
        default_analyst = _default_tier_name(tiers, "analyst")
        org_names = {o.name for o in orgs}
        for b in bindings:
            if b.group not in group_hashes:
                continue  # no minted secret (no secrets store) -> skip the user
            tier_name = b.tier or default_analyst
            tier_role = f"dfe_{tier_name}_role"
            org_role = org_role_name(b.org) if (b.org and b.org in org_names) else None
            stmts += render_group_user(
                b.user(), group_hashes[b.group], tier_role=tier_role, org_role=org_role
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
        if self._secrets is not None:
            for r in service_roles:
                if r.mint_user:
                    service_hashes[r.name] = self._hash_for(f"ch/service/{r.name}")
                    result.minted.append(f"service/{r.name}")
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
        )
        drops = compute_drops(
            self._existing_org_roles(), self._existing_row_policies(), orgs, org_tables
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
    (e.g. once tiers/bindings load from gitcrud). ``secrets_store`` (the
    scalo.secrets seam) enables minting the service/group USERS - without it only
    tiers, roles and org row policies reconcile. Used by app.py startup, the
    ``reconcile-ch-rbac`` CLI command, and the governance API endpoint.
    """
    return ChRbacReconciler(admin_client, secrets_store=secrets_store).reconcile(
        tiers=tiers if tiers is not None else DEFAULT_TIERS,
        service_roles=service_roles if service_roles is not None else DEFAULT_SERVICE_ROLES,
        orgs=orgs or [],
        bindings=bindings or [],
    )
