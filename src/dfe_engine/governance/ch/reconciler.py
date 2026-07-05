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
DDL, and drop stale objects. Tenant isolation is the custom-settings model - a
fixed set of users by privilege + ONE ``DFE_current_tenant_id``-driven row policy
per ``_org_id`` table (render_fixed_users / render_tenant_policies) - so adding an
org is zero DDL, and the drop pass also cleans up the RETIRED per-org roles /
per-(org,table) policies from the previous model. Every statement is idempotent
(``IF NOT EXISTS`` / ``OR REPLACE`` / computed diff for drops), so a second run is
a no-op. See docs/superpowers/specs/2026-07-01-ch-org-rbac-quota-tiers-design.md.
"""

from __future__ import annotations

import hashlib
import secrets as _stdlib_secrets
from typing import Any

from pydantic import BaseModel, Field
from scalo.logger import logger

from .models import (
    DEFAULT_SERVICE_ROLES,
    DEFAULT_TIERS,
    FIXED_USERS,
    TENANT_POLICY_NAME,
    ChServiceRole,
    ChTier,
    GroupChBinding,
)
from .render import (
    _bq,
    render_fixed_users,
    render_materialise,
    render_service_role,
    render_service_user,
    render_tenant_policies,
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


def compute_drops(
    existing_org_roles: set[str],
    existing_policies: list[tuple[str, str, str]],
    org_tables: list[tuple[str, str]],
) -> list[str]:
    """Pure diff: DROP DDL for stale tenant policies + the retired per-org objects.

    Two sources of staleness:
    - a ``dfe_tenant_filter`` policy on a table that no longer carries ``_org_id``
      (the table lost the column or was dropped), and
    - every object from the RETIRED per-org model: ``dfe_rowpol_*`` policies and
      ``dfe_org_*`` roles, which the fixed-user + single-policy model replaces
      wholesale (dropped unconditionally - none are desired any more).

    Policies drop before roles (a policy targets a role). Only touches
    ``dfe_tenant_filter`` / ``dfe_rowpol_`` / ``dfe_org_`` objects, so hand-made CH
    objects and the fixed users are safe. ``existing_policies`` is
    ``(short_name, db, table)`` from system.row_policies.
    """
    desired_tenant = set(org_tables)
    drops: list[str] = []
    for short_name, db, table in existing_policies:
        stale_tenant = short_name == TENANT_POLICY_NAME and (db, table) not in desired_tenant
        if short_name.startswith("dfe_rowpol_") or stale_tenant:
            drops.append(f"DROP ROW POLICY IF EXISTS {_bq(short_name)} ON {_bq(db)}.{_bq(table)}")
    for role in sorted(existing_org_roles):
        if role.startswith("dfe_org_"):
            drops.append(f"DROP ROLE IF EXISTS {_bq(role)}")
    return drops


def bindings_from_groups(groups: list[Any], orgs: list[Any]) -> list[GroupChBinding]:
    """Resolve each auth group to the ONE org it maps to (group -> org pointer).

    Under the fixed-user model the reconciler no longer mints a CH user per group;
    this resolver survives ONLY to feed the HyperDX connection builder + org
    lifecycle, which still need "which single org does this group grant". Each
    resolvable group yields one ``GroupChBinding`` with ``binding.org`` = the
    resolved Org NAME (Phase 3 will map that org to a ``DFE_current_tenant_id`` on
    the shared ``dfe_tenant_reader`` connection instead of a per-group user).

    Resolution is convention-agnostic: an org is indexed by BOTH its name and each
    of its tenant ``org_ids``, so a group whose ``org_ids`` hold org names OR tenant
    ids resolves either way (the common name==org_id case is unambiguous). Rules:

    - a group with NO ``org_ids`` yields no binding (no org-scoped data access), and
    - a group whose ``org_ids`` resolve to MORE THAN ONE distinct org is skipped:
      the model carries at most one org per group, so we refuse rather than mislabel
      it (a multi-org tenant view is the caller's explicit comma-joined list, not an
      accidental group mapping).

    ``tier`` is left empty (vestigial). Pure; duck-typed on ``.name`` / ``.org_ids``
    so it never imports the auth Group type.
    """
    index: dict[str, str] = {}
    for o in orgs:
        index.setdefault(o.name, o.name)
        for oid in getattr(o, "org_ids", None) or []:
            index.setdefault(oid, o.name)

    bindings: list[GroupChBinding] = []
    for g in groups:
        refs = list(getattr(g, "org_ids", None) or [])
        if not refs:
            continue  # no org-scoped access -> no CH data user derived
        resolved = {index[r] for r in refs if r in index}
        if len(resolved) != 1:
            continue  # unresolved OR ambiguous (>1 org) -> refuse (fail closed)
        (org_name,) = tuple(resolved)
        bindings.append(GroupChBinding(group=g.name, org=org_name))
    return bindings


def load_catalogue_from_gitcrud(handle: Any) -> tuple[list[ChTier], list[ChServiceRole]]:
    """Load the CH tier + service-role catalogue from a gitcrud handle (the e join).

    Reads the ``ch_tiers`` + ``ch_service_roles`` resource classes (gitcrud/registry)
    into ``ChTier`` / ``ChServiceRole``. Both classes are versioned, so a resource's
    PUBLISHED spec is its payload (via VersionedDoc); an unversioned class would read
    the raw doc. Returns ``(tiers, service_roles)``; either may be empty (the caller
    falls back to the seeded ``DEFAULT_*``). Kept OPTIONAL: the import is lazy so the
    reconciler still runs seed-only without a gitcrud handle, and a resource with no
    published version is skipped rather than erroring.
    """
    from dfe_engine.gitcrud import VersionedDoc  # lazy: gitcrud stays an optional dep

    def _load(cls_name: str, model: Any) -> list[Any]:
        versioned = handle.resource_class(cls_name).versioned
        vdoc = VersionedDoc(handle) if versioned else None
        out: list[Any] = []
        for name in handle.list(cls_name):
            spec = vdoc.get_published(cls_name, name) if versioned else handle.get(cls_name, name)
            if spec:
                out.append(model.model_validate(spec))
        return out

    return _load("ch_tiers", ChTier), _load("ch_service_roles", ChServiceRole)


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
        # Find BOTH the current tenant policy (dfe_tenant_filter - to drop it off a
        # table that no longer carries _org_id) and the retired dfe_rowpol_*
        # policies (to clean them up wholesale).
        rows = self._client.query(
            "SELECT short_name, database, table FROM system.row_policies "
            "WHERE short_name LIKE 'dfe_rowpol_%' OR short_name = {tf:String}",
            parameters={"tf": TENANT_POLICY_NAME},
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
        org_tables: list[tuple[str, str]],
        service_hashes: dict[str, str],
        fixed_hashes: dict[str, str],
    ) -> list[str]:
        """Full ordered DDL: quota tiers -> service roles (+ minted service users)
        -> fixed users by privilege -> ONE tenant row policy per ``_org_id`` table.

        Pure given the discovered ``org_tables`` + the minted ``*_hashes``. Order is
        load-bearing at the boundary: the fixed users (incl. ``dfe_tenant_reader``)
        are created BEFORE the tenant policies that target the reader.
        """
        stmts: list[str] = []
        for t in tiers:
            stmts += render_tier(t)
        for r in service_roles:
            stmts += render_service_role(r)
            if r.mint_user and r.name in service_hashes:
                stmts += render_service_user(r, service_hashes[r.name])
        stmts += render_fixed_users(fixed_hashes)
        stmts += render_tenant_policies(org_tables)
        return stmts

    # ---- reconcile -------------------------------------------------------

    def reconcile(
        self,
        *,
        tiers: list[Any],
        service_roles: list[Any],
        orgs: list[Any],
    ) -> ReconcileResult:
        """Discover, mint, render, apply, and drop-stale, idempotently.

        ``orgs`` now drives ONLY the ``dfe_meta.orgs`` projection (materialise): the
        tenant axis is the fixed reader + one per-table policy, so NO per-org CH
        objects are created and NO group bindings are minted. The fixed users
        (dfe_analyst / dfe_analyst_ro / dfe_tenant_reader) are minted whenever a
        secrets store is present.
        """
        result = ReconcileResult()
        org_tables = self.discover_org_id_tables()

        service_hashes: dict[str, str] = {}
        fixed_hashes: dict[str, str] = {}
        if self._secrets is not None:
            for r in service_roles:
                if r.mint_user:
                    service_hashes[r.name] = self._hash_for(f"ch/service/{r.name}")
                    result.minted.append(f"service/{r.name}")
            for fu in FIXED_USERS:
                fixed_hashes[fu.name] = self._hash_for(f"ch/fixed/{fu.name}")
                result.minted.append(f"fixed/{fu.name}")

        stmts = self.render_all(
            tiers=tiers,
            service_roles=service_roles,
            org_tables=org_tables,
            service_hashes=service_hashes,
            fixed_hashes=fixed_hashes,
        )
        drops = compute_drops(self._existing_org_roles(), self._existing_row_policies(), org_tables)
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
    gitcrud: Any = None,
) -> ReconcileResult:
    """The one entry point to reconcile the CH-RBAC config into ClickHouse.

    Catalogue precedence (tiers + service roles): an EXPLICIT ``tiers`` /
    ``service_roles`` arg wins; else, when a ``gitcrud`` handle is given, the
    ``ch_tiers`` / ``ch_service_roles`` classes are loaded from git and used WHEN
    NON-EMPTY (models.py: 'the catalogue is config, never a hardcoded list'); else
    the SEEDED ``DEFAULT_*`` are used. ``gitcrud`` is OPTIONAL - seed-only still
    works without it.

    ``secrets_store`` (the scalo.secrets seam) enables minting the service + fixed
    USERS - without it only tiers, roles and the tenant row policies reconcile.
    ``orgs`` drives only the ``dfe_meta.orgs`` projection (the tenant axis is the
    fixed reader + per-table policy, no per-org DDL). Used by app.py startup, the
    ``reconcile-ch-rbac`` CLI command, and the governance API endpoint.
    """
    if gitcrud is not None and (tiers is None or service_roles is None):
        loaded_tiers, loaded_roles = load_catalogue_from_gitcrud(gitcrud)
        if tiers is None and loaded_tiers:
            tiers = loaded_tiers
        if service_roles is None and loaded_roles:
            service_roles = loaded_roles
    return ChRbacReconciler(admin_client, secrets_store=secrets_store).reconcile(
        tiers=tiers if tiers is not None else DEFAULT_TIERS,
        service_roles=service_roles if service_roles is not None else DEFAULT_SERVICE_ROLES,
        orgs=orgs or [],
    )
