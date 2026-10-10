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
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field
from scalo.logger import logger

from dfe_engine.clickhouse.errors import is_connection_error
from dfe_engine.orgs.tenant_scope import org_tenant_ids

from .models import (
    DB,
    DEFAULT_SERVICE_ROLES,
    DEFAULT_TIERS,
    GROUP_USER_PREFIX,
    org_user_name,
    tenant_policy_name,
)
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


def resolve_grant_databases(items: list[Any], database: str) -> list[Any]:
    """Copies of *items* with the ``{db}`` placeholder in their grants resolved.

    The tier and service-role seeds name no database (``models.DB``), so this is
    what turns them into grants against the deployment's actual one. Copies, so a
    caller's config objects are never mutated and a second reconcile re-resolves
    from the same source.
    """
    resolved: list[Any] = []
    for item in items:
        grants = getattr(item, "grants", None)
        if not grants or not any(DB in g for g in grants):
            resolved.append(item)
            continue
        resolved.append(
            item.model_copy(update={"grants": [g.replace(DB, database) for g in grants]})
        )
    return resolved


def _default_tier_name(tiers: list[Any], kind: str) -> str:
    """Name of the default tier for a kind (the flagged default, else the first)."""
    of_kind = [t for t in tiers if t.kind == kind]
    for t in of_kind:
        if t.default:
            return t.name
    return of_kind[0].name if of_kind else ""


def _tenant_granted_dbs(tiers: list[Any]) -> list[str]:
    """Databases a tenant (pinned) user can SELECT, from the analyst-tier grants.

    Pinned users hold an analyst tier whose grants are ``SELECT ON <db>.*`` (the
    broad data-db grant, D9). A table in one of those dbs that carries no
    ``_org_id`` gets no ``_org_id`` row policy, so the tenant would read it in full
    - this list bounds where that class can hide (``dfe``'s ``otel_*`` etc.) and
    feeds the deny policies rendered by ``render_tenant_axis``.
    """
    dbs: set[str] = set()
    for t in tiers:
        if getattr(t, "kind", "analyst") != "analyst":
            continue
        for grant in getattr(t, "grants", []):
            head, _sep, target = grant.partition(" ON ")
            if head.strip().upper().startswith("SELECT") and target.endswith(".*"):
                dbs.add(target[:-2].strip())
    return sorted(dbs)


def compute_drops(
    existing_org_roles: set[str],
    existing_policies: list[tuple[str, str, str]],
    existing_org_users: set[str],
    orgs: list[Any],
    org_tables: list[tuple[str, str]],
    deny_tables: list[tuple[str, str]] | None = None,
    existing_group_users: set[str] | None = None,
    bindings: list[Any] | None = None,
) -> list[str]:
    """Pure diff: DROP DDL for tenant objects with no config behind them.

    Four sweeps, each prefix-scoped so hand-made CH objects are safe:

    - ``dfe_rowpol_*`` policies not in the desired tenant set. This also retires
      the pre-pinned design's per-org literal policies on upgrade.
    - ``dfe_org_*`` roles - ALL of them: the pinned design has no per-org roles,
      so any survivor is the old design's leftover.
    - ``dfe_org_*`` users for orgs no longer registered. An offboarded org whose
      credential stays live is still a tenant of the platform; the drop is the
      revocation.
    - ``dfe_grp_*`` users no binding renders any more: the group was deleted, or
      now claims an org that is unregistered or ambiguous. A platform group's
      user reads every org's rows, so leaving it live after the group goes is a
      credential nothing governs.

    ``existing_policies`` is ``(short_name, db, table)`` from system.row_policies.
    """
    desired_policies = {tenant_policy_name(db, t) for (db, t) in org_tables}
    desired_policies |= {tenant_policy_name(db, t) for (db, t) in (deny_tables or [])}
    desired_users = {org_user_name(o.name) for o in orgs}
    desired_group_users = {b.user() for b in bindings or []}
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
    for user in sorted(existing_group_users or set()):
        if user.startswith(GROUP_USER_PREFIX) and user not in desired_group_users:
            drops.append(f"DROP USER IF EXISTS {_bq(user)}")
    return drops


_STATEMENT_HEAD_CHARS = 60


def _statement_head(stmt: str) -> str:
    """The start of ``stmt``, cut before any ``IDENTIFIED`` clause so no password hash shows."""
    head = stmt.split(" IDENTIFIED ", 1)[0]
    if len(head) <= _STATEMENT_HEAD_CHARS:
        return head
    return f"{head[:_STATEMENT_HEAD_CHARS]}..."


class ChRbacReconciler:
    """Reconcile the gitops CH-RBAC config into real ClickHouse objects.

    ``admin_client`` is a clickhouse-connect client with admin rights (duck-typed:
    ``.command(stmt)`` + ``.query(sql, parameters=...).result_rows``).
    ``secrets_store`` is the scalo.secrets seam (``DfeSecrets``): when absent, tiers
    / roles / org policies still reconcile but no group/org USERS are minted.
    ``database`` is what the ``{db}`` placeholder in the seeded grants resolves to,
    defaulting to the deployment's data database. ``provided_passwords`` maps a
    service role name to the password its deployment supplies, which that minted
    user adopts in place of one the engine mints.
    """

    def __init__(
        self,
        admin_client: Any,
        *,
        secrets_store: Any = None,
        database: str | None = None,
        provided_passwords: Mapping[str, str] | None = None,
    ) -> None:
        self._client = admin_client
        self._secrets = secrets_store
        self._database = database or self._settings_database()
        self._provided = dict(provided_passwords or {})

    @staticmethod
    def _settings_database() -> str:
        """The deployment's data database, or the model default when unloadable."""
        try:
            from dfe_engine.settings import get_settings

            return get_settings().clickhouse.effective_data_database
        except Exception:
            from dfe_engine.settings import ClickHouseSettings

            return str(ClickHouseSettings.model_fields["data_database"].default)

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

    def discover_tenant_reachable_tables(self, dbs: list[str]) -> list[tuple[str, str]]:
        """(db, table) for policy-applicable tables in the tenant-granted dbs.

        Excludes views, dictionaries, temporaries and MV inner tables - a row
        policy only attaches to a real table. The caller deny-policies whichever
        of these carry no ``_org_id``.
        """
        if not dbs:
            return []
        rows = self._client.query(
            "SELECT database, name FROM system.tables "
            "WHERE database IN {dbs:Array(String)} "
            "AND engine NOT LIKE '%View%' AND engine != 'Dictionary' "
            "AND is_temporary = 0 AND name NOT LIKE '.inner%' "
            "ORDER BY database, name",
            parameters={"dbs": dbs},
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

    def _existing_group_users(self) -> set[str]:
        rows = self._client.query(
            "SELECT name FROM system.users WHERE startsWith(name, {prefix:String})",
            parameters={"prefix": GROUP_USER_PREFIX},
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

    def _service_hash(self, role: Any) -> str | None:
        """sha256 for a minted service user, or None when there is no password to set.

        A password the deployment provides wins over mint-or-reuse: a separate pod
        connects with that value, so the user has to carry it. It is also written
        to the store, so a reader of the stored secret gets the password that works.
        """
        path = role.secret_path()
        provided = self._provided.get(role.name, "")
        if not provided:
            return self._hash_for(path) if self._secrets is not None else None
        if self._secrets is not None:
            stored = self._secrets.get(path) if self._secrets.exists(path) else None
            if stored != provided:
                self._secrets.put(path, provided)
        return hashlib.sha256(provided.encode()).hexdigest()

    def _service_hashes(self, service_roles: list[Any], result: ReconcileResult) -> dict[str, str]:
        """Hash every minted service user that has a password, recording each in ``result``."""
        hashes: dict[str, str] = {}
        for r in service_roles:
            if not r.mint_user:
                continue
            pw_hash = self._service_hash(r)
            if pw_hash is None:
                continue
            hashes[r.name] = pw_hash
            result.minted.append(f"service/{r.name}")
        return hashes

    def _apply(self, stmts: list[str], result: ReconcileResult) -> None:
        """Run each statement, recording a failure and carrying on with the rest.

        A lost connection is not a bad statement: it raises, so the run reads as
        failed and is retried, rather than as a partial run nobody runs again.

        ClickHouse's error text goes to the log only. ``result.errors`` reaches API
        callers, so it names the statement up to any password hash and nothing more.
        """
        for stmt in stmts:
            try:
                self._client.command(stmt)
            except Exception as exc:  # a bad statement must not abort the rest
                if is_connection_error(exc):
                    raise
                head = _statement_head(stmt)
                logger.warning("CH RBAC statement failed", statement=head, error=str(exc))
                result.errors.append(f"{head} failed; the engine log has ClickHouse's reason")

    # ---- pure render -----------------------------------------------------

    @staticmethod
    def _render_service_identities(
        service_roles: list[Any], service_hashes: dict[str, str]
    ) -> list[str]:
        """Each service role, then its minted user where it has a password hash."""
        stmts: list[str] = []
        for r in service_roles:
            stmts += render_service_role(r)
            if r.mint_user and r.name in service_hashes:
                stmts += render_service_user(r, service_hashes[r.name])
        return stmts

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
        deny_tables: list[tuple[str, str]] | None = None,
    ) -> list[str]:
        """Full ordered DDL (spec 7): tiers -> service roles -> tenant axis ->
        org users -> group users. Pure given the discovered ``org_tables`` and the
        minted ``*_hashes``.

        Resolves ``{db}`` in the grants, so calling this directly on the seeds
        renders the same DDL ``reconcile`` applies.
        """
        tiers = resolve_grant_databases(tiers, self._database)
        service_roles = resolve_grant_databases(service_roles, self._database)
        stmts: list[str] = []
        for t in tiers:
            stmts += render_tier(t)
        stmts += self._render_service_identities(service_roles, service_hashes)
        default_analyst = _default_tier_name(tiers, "analyst")
        default_tier_role = f"dfe_{default_analyst}_role"
        stmts += render_tenant_axis(org_tables, deny_tables)
        # The org's pinned user: the identity its hyperdx team connects as.
        orgs_by_name = {o.name: o for o in orgs}
        for o in orgs:
            if o.name not in org_hashes:
                continue  # no minted secret (no secrets store) -> skip the user
            stmts += render_pinned_user(
                org_user_name(o.name),
                org_hashes[o.name],
                tier_role=default_tier_role,
                org_ids=org_tenant_ids(o),
            )
        for b in bindings:
            if b.group not in group_hashes:
                continue
            tier_role = f"dfe_{b.tier or default_analyst}_role"
            org = orgs_by_name.get(b.org) if b.org else None
            org_ids = org_tenant_ids(org) if org is not None else []
            stmts += render_pinned_user(
                b.user(),
                group_hashes[b.group],
                tier_role=tier_role,
                org_ids=org_ids,
                extra_roles=[f"dfe_{r}_role" for r in getattr(b, "ch_roles", [])],
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
        # Resolve {db} before anything reads the grants: the granted-db list the
        # deny policies are built from is parsed straight out of them.
        tiers = resolve_grant_databases(tiers, self._database)
        service_roles = resolve_grant_databases(service_roles, self._database)
        org_tables = self.discover_org_id_tables()
        org_set = set(org_tables)
        deny_tables = [
            t
            for t in self.discover_tenant_reachable_tables(_tenant_granted_dbs(tiers))
            if t not in org_set
        ]
        if deny_tables:
            logger.info(
                "tenant deny-policy over non-_org_id tables in granted dbs",
                count=len(deny_tables),
            )

        service_hashes = self._service_hashes(service_roles, result)
        group_hashes: dict[str, str] = {}
        org_hashes: dict[str, str] = {}
        if self._secrets is not None:
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
            deny_tables=deny_tables,
        )
        drops = compute_drops(
            self._existing_org_roles(),
            self._existing_row_policies(),
            self._existing_org_users(),
            orgs,
            org_tables,
            deny_tables,
            existing_group_users=self._existing_group_users(),
            bindings=bindings,
        )
        materialise = render_materialise(orgs, tiers)

        self._apply(stmts + drops + materialise, result)
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

    def reconcile_service_roles(self, service_roles: list[Any]) -> ReconcileResult:
        """Reconcile the service roles and their minted users alone.

        The service identities are least privilege for the engine's own workers,
        not tenant fencing, so a deployment with tenant isolation off still gets
        them. Touches no tier, org, group or row policy, and drops nothing.
        """
        result = ReconcileResult()
        service_roles = resolve_grant_databases(service_roles, self._database)
        stmts = self._render_service_identities(
            service_roles, self._service_hashes(service_roles, result)
        )
        self._apply(stmts, result)
        result.statements = stmts
        if result.errors:
            logger.warning(
                "CH service roles reconciled with errors", error_count=len(result.errors)
            )
        else:
            logger.info(
                "CH service roles reconciled", statements=len(stmts), minted=len(result.minted)
            )
        return result


def fence_tables(admin_client: Any, *, tiers: list[Any] | None = None, database: str = "") -> int:
    """Re-apply the tenant row policies alone, and report how many statements ran.

    Sources create their table one-per-source at deploy time, and the tier grant is
    database-wide, so a new table is readable the instant it exists; only its row
    policy waits for a reconcile. Running this on deploy closes that window. Mints
    nothing and touches no user, so it is cheap enough to run every time.
    """
    reconciler = ChRbacReconciler(admin_client, database=database or None)
    resolved = resolve_grant_databases(list(tiers or DEFAULT_TIERS), reconciler._database)
    org_tables = reconciler.discover_org_id_tables()
    org_set = set(org_tables)
    deny_tables = [
        t
        for t in reconciler.discover_tenant_reachable_tables(_tenant_granted_dbs(resolved))
        if t not in org_set
    ]

    applied = 0
    for stmt in render_tenant_axis(org_tables, deny_tables):
        try:
            admin_client.command(stmt)
            applied += 1
        except Exception as exc:
            logger.warning("tenant fence statement failed", statement=stmt[:60], error=str(exc))
    logger.info(
        "tenant fence applied",
        statements=applied,
        org_tables=len(org_tables),
        deny_tables=len(deny_tables),
    )
    return applied


def reconcile_ch_rbac(
    admin_client: Any,
    *,
    secrets_store: Any = None,
    orgs: list[Any] | None = None,
    tiers: list[Any] | None = None,
    service_roles: list[Any] | None = None,
    bindings: list[Any] | None = None,
    provided_passwords: Mapping[str, str] | None = None,
) -> ReconcileResult:
    """The one entry point to reconcile the CH-RBAC config into ClickHouse.

    Defaults to the SEEDED tiers + service roles; pass explicit lists to override
    (e.g. once tiers load from gitcrud). ``bindings`` come from
    ``derive_group_bindings`` over the RBAC group store. ``secrets_store`` (the
    scalo.secrets seam) enables minting the service/group USERS - without it only
    tiers, roles, org row policies and service users with a ``provided_passwords``
    entry reconcile. Used by app.py startup and the governance API endpoint.
    """
    reconciler = ChRbacReconciler(
        admin_client, secrets_store=secrets_store, provided_passwords=provided_passwords
    )
    return reconciler.reconcile(
        tiers=tiers if tiers is not None else DEFAULT_TIERS,
        service_roles=service_roles if service_roles is not None else DEFAULT_SERVICE_ROLES,
        orgs=orgs or [],
        bindings=bindings or [],
    )


def reconcile_ch_service_roles(
    admin_client: Any,
    *,
    secrets_store: Any = None,
    service_roles: list[Any] | None = None,
    provided_passwords: Mapping[str, str] | None = None,
) -> ReconcileResult:
    """Reconcile the service roles and their users alone, defaulting to the seeded set.

    What app.py startup runs when tenant isolation is off, so the engine's workers
    still get their least-privilege identities.
    """
    reconciler = ChRbacReconciler(
        admin_client, secrets_store=secrets_store, provided_passwords=provided_passwords
    )
    return reconciler.reconcile_service_roles(
        service_roles if service_roles is not None else DEFAULT_SERVICE_ROLES
    )
