#  Project:      dfe-engine
#  File:         governance/ch_rbac.py
#  Purpose:      Per-group ClickHouse identity (grants + quota + settings profile)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One ClickHouse user per RBAC group, carrying THREE controls (see
docs memory project_hyperdx_ch_rbac): GRANTs (what data), a SETTINGS PROFILE
(per-query limits), and a QUOTA (rate/volume). CH is the authoritative + safety
boundary - the HyperDX app layer cannot do quotas/limits.

GENERATED AS GITOPS DDL, the SAME WAY as schema DDL (Derek): a read-only CORE of
default user-creation TIERS lives in dfe-schemas / dfe-infra (user-supplied), and
the per-group OVERLAY is emitted by the engine into the deploy repo as a DDL
artifact (``ddl/ch-rbac/<group>.sql``). Both are applied by the shared migration
runner - NOT by imperative engine SQL. A group's binding may reference a core
``tier`` and add specifics. The imperative ``GroupChProvisioner`` below is the
secondary "dormant/direct" path; the gitops-DDL artifact is primary.

HyperDX selects the connection bound to the user's group (fork change, separate);
this module owns the CH side only.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from dfe_engine.secrets import DfeSecrets


class GroupChBinding(BaseModel):
    """A group's ClickHouse identity: data scope + per-query limits + quota.

    ``tier`` references a core user-creation tier (default DDL supplied in
    dfe-schemas/dfe-infra); the remaining fields are the per-group overlay.
    """

    group: str
    ch_user: str = ""  # defaults to dfe_grp_<group>
    tier: str = ""  # optional reference to a core tier (readonly/readwrite/admin)
    grants: list[str] = Field(default_factory=list)  # e.g. ["SELECT ON dfe.*"]
    # per-query limits (settings profile)
    settings: dict[str, int] = Field(default_factory=dict)
    # quota: interval + maxima
    quota_interval: str = "1 hour"
    quota: dict[str, int] = Field(default_factory=dict)  # queries/result_rows/execution_time/errors

    def user(self) -> str:
        return self.ch_user or f"dfe_grp_{self.group}"

    def profile(self) -> str:
        return f"dfe_grp_{self.group}_profile"

    def quota_name(self) -> str:
        return f"dfe_grp_{self.group}_quota"


def generate_password() -> str:
    """A 32-char URL-safe password (same shape as the org provisioner)."""
    return secrets.token_urlsafe(24)[:32]


def build_group_sql(binding: GroupChBinding, password_hash: str) -> list[str]:
    """CH DDL for the group's user + grants + settings profile + quota (idempotent).

    Identifiers are backtick-quoted so group names with hyphens (e.g. ``soc-ro``)
    produce valid ClickHouse names.
    """
    qu = f"`{binding.user()}`"
    stmts: list[str] = [
        f"CREATE USER IF NOT EXISTS {qu} IDENTIFIED WITH sha256_hash BY '{password_hash}'"
    ]
    for grant in binding.grants:
        stmts.append(f"GRANT {grant} TO {qu}")

    if binding.settings:
        prof = f"`{binding.profile()}`"
        kv = ", ".join(f"{k} = {v}" for k, v in sorted(binding.settings.items()))
        stmts.append(f"CREATE SETTINGS PROFILE IF NOT EXISTS {prof} SETTINGS {kv}")
        stmts.append(f"ALTER USER {qu} SETTINGS PROFILE {prof}")

    if binding.quota:
        qn = f"`{binding.quota_name()}`"
        maxima = ", ".join(f"{k} = {v}" for k, v in sorted(binding.quota.items()))
        stmts.append(
            f"CREATE QUOTA IF NOT EXISTS {qn} "
            f"FOR INTERVAL {binding.quota_interval} MAX {maxima} TO {qu}"
        )
    return stmts


def ddl_artifact(binding: GroupChBinding, password_hash: str) -> tuple[str, str]:
    """Render the per-group CH user as a GITOPS DDL artifact (primary path).

    Returns (repo-relative path, SQL text) for the deploy repo, applied by the
    shared migration runner alongside schema DDL. The core default-tier DDL is
    supplied separately in dfe-schemas/dfe-infra; this is the per-group overlay.
    """
    header = (
        f"-- DFE Governed Ops: ClickHouse identity for group '{binding.group}'\n"
        f"-- tier: {binding.tier or '(none)'} - applied by the shared migration runner\n"
    )
    sql = header + ";\n".join(build_group_sql(binding, password_hash)) + ";\n"
    return f"ddl/ch-rbac/{binding.group}.sql", sql


def mint_group_secret(binding: GroupChBinding, secrets_store: DfeSecrets) -> tuple[str, str]:
    """Mint a group's CH password: plaintext to the secrets seam, hash to gitops DDL.

    The generated password goes to ``scalo.secrets`` (file / openbao / cloud, per
    config) under ``ch/groups/<group>``; the deploy-repo DDL artifact carries ONLY
    the sha256 hash. The plaintext NEVER lands in git. Returns the same
    (repo-relative path, SQL) tuple as ``ddl_artifact``.
    """
    password = generate_password()
    secrets_store.put(f"ch/groups/{binding.group}", password)
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    return ddl_artifact(binding, pw_hash)


class GroupChProvisioner:
    """Apply a GroupChBinding to ClickHouse (idempotent). Returns the password.

    Secondary "dormant/direct" path; the gitops-DDL artifact (ddl_artifact) is
    primary.
    """

    def __init__(self, ch_client) -> None:
        self._client = ch_client

    def provision(self, binding: GroupChBinding) -> tuple[bool, str]:
        password = generate_password()
        pw_hash = hashlib.sha256(password.encode()).hexdigest()
        for stmt in build_group_sql(binding, pw_hash):
            self._client.command(stmt)
        return True, password
