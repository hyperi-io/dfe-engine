#  Project:      dfe-engine
#  File:         src/dfe_engine/auth/audit.py
#  Purpose:      SOC2 audit event emitters for authentication and authorisation decisions.
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""SOC2 audit logging via structured log events.

Each function emits a single structured log event. Events flow through the
OTel pipeline automatically: hyperi_pylib.logger → structured JSON →
OTel Collector → ClickHouse → HyperDX.

No custom ClickHouse tables are needed — the log events are the audit trail.

Event naming convention: ``auth.<domain>.<outcome>``
- Success events: ``logger.info``
- Denied/failure events: ``logger.warning``
"""

from __future__ import annotations

from hyperi_pylib.logger import logger


def audit_login_success(
    user_id: str,
    auth_path: str,
    client_ip: str | None,
    roles: list[str],
) -> None:
    """Emit an audit event for a successful authentication.

    Args:
        user_id: Authenticated identity (sub, ``apikey:<name>``, etc.).
        auth_path: Which auth path succeeded (``oidc``, ``api_key``, ``jwt``, ``disabled``).
        client_ip: Client IP address, or None if unavailable.
        roles: Roles resolved for this identity.
    """
    logger.info(
        "auth.login.success",
        user_id=user_id,
        auth_path=auth_path,
        client_ip=client_ip,
        roles=roles,
    )


def audit_login_denied(
    user_id: str,
    auth_path: str,
    client_ip: str | None,
    reason: str,
) -> None:
    """Emit an audit event for a failed or denied authentication attempt.

    Args:
        user_id: Best-effort identity (key prefix, ``anonymous``, etc.).
        auth_path: Which auth path was attempted (``oidc``, ``api_key``, ``jwt``, ``none``).
        client_ip: Client IP address, or None if unavailable.
        reason: Short machine-readable reason (e.g. ``invalid_key``, ``no_credentials``).
    """
    logger.warning(
        "auth.login.denied",
        user_id=user_id,
        auth_path=auth_path,
        client_ip=client_ip,
        reason=reason,
    )


def audit_permission_denied(
    user_id: str,
    action: str,
    roles: list[str],
    reason: str,
) -> None:
    """Emit an audit event when an authorisation check fails.

    Args:
        user_id: Authenticated identity attempting the action.
        action: The action that was denied (e.g. ``source:write``).
        roles: Roles held by the user at time of denial.
        reason: Human-readable reason from the authorisation engine.
    """
    logger.warning(
        "auth.permission.denied",
        user_id=user_id,
        action=action,
        roles=roles,
        reason=reason,
    )


def audit_account_change(
    admin_id: str,
    target_user: str,
    change: str,
) -> None:
    """Emit an audit event for a local account create/update/delete.

    Args:
        admin_id: Identity of the admin performing the change.
        target_user: Username of the affected account.
        change: One of ``"created"``, ``"updated"``, ``"deleted"``.
    """
    logger.info(
        f"auth.account.{change}",
        admin_id=admin_id,
        target_user=target_user,
        change=change,
    )


def audit_group_change(
    admin_id: str,
    group_name: str,
    change: str,
) -> None:
    """Emit an audit event for a group create/update/delete.

    Args:
        admin_id: Identity of the admin performing the change.
        group_name: Name of the affected group.
        change: One of ``"created"``, ``"updated"``, ``"deleted"``.
    """
    logger.info(
        f"auth.group.{change}",
        admin_id=admin_id,
        group_name=group_name,
        change=change,
    )


def audit_api_key_change(
    admin_id: str,
    key_name: str,
    short_token: str,
    change: str,
) -> None:
    """Emit an audit event for an API key create/revoke.

    Args:
        admin_id: Identity of the admin performing the change.
        key_name: Human-readable name of the API key.
        short_token: First 16 characters of the token for correlation (safe to log).
        change: One of ``"created"``, ``"revoked"``.
    """
    logger.info(
        f"auth.api_key.{change}",
        admin_id=admin_id,
        key_name=key_name,
        short_token=short_token,
        change=change,
    )


def audit_org_change(
    admin_id: str,
    org_name: str,
    change: str,
    details: dict | None = None,
) -> None:
    """Emit an audit event for an org create/update/delete.

    Args:
        admin_id: Identity of the admin performing the change.
        org_name: Name of the affected org.
        change: One of ``"created"``, ``"updated"``, ``"deleted"``.
        details: Optional additional structured details.
    """
    logger.info(
        f"org.{change}", admin_id=admin_id, org_name=org_name, change=change, details=details
    )


def audit_org_ch_provisioned(org_name: str, ch_user: str, databases: list[str]) -> None:
    """Emit an audit event when a ClickHouse user is created for an org.

    Args:
        org_name: Name of the org being provisioned.
        ch_user: ClickHouse username created.
        databases: List of databases the user was granted access to.
    """
    logger.info("org.ch.user_created", org_name=org_name, ch_user=ch_user, databases=databases)


def audit_org_ch_failed(org_name: str, error: str) -> None:
    """Emit an audit event when ClickHouse provisioning fails for an org.

    Args:
        org_name: Name of the org that failed provisioning.
        error: Error description.
    """
    logger.warning("org.ch.provision_failed", org_name=org_name, error=error)


def audit_org_hyperdx_provisioned(org_name: str, team_id: str) -> None:
    """Emit an audit event when a HyperDX team is created for an org.

    Args:
        org_name: Name of the org being provisioned.
        team_id: HyperDX team ID created.
    """
    logger.info("org.hyperdx.team_created", org_name=org_name, team_id=team_id)


def audit_org_hyperdx_failed(org_name: str, error: str) -> None:
    """Emit an audit event when HyperDX provisioning fails for an org.

    Args:
        org_name: Name of the org that failed provisioning.
        error: Error description.
    """
    logger.warning("org.hyperdx.provision_failed", org_name=org_name, error=error)


def audit_jit_account_created(
    user_id: str,
    source_provider: str,
    groups: list[str],
    org_ids: list[str],
) -> None:
    """Emit an audit event when a JIT account is created on first OIDC login.

    Args:
        user_id: Identity of the newly created account.
        source_provider: OIDC provider name that triggered creation.
        groups: Groups resolved from the OIDC provider at creation time.
        org_ids: Org IDs the account was assigned to.
    """
    logger.info(
        "auth.jit.account_created",
        user_id=user_id,
        source_provider=source_provider,
        groups=groups,
        org_ids=org_ids,
    )


def audit_jit_groups_updated(user_id: str, added: list[str], removed: list[str]) -> None:
    """Emit an audit event when a JIT account's groups are updated on re-login.

    Args:
        user_id: Identity of the account being updated.
        added: Groups added since last login.
        removed: Groups removed since last login.
    """
    logger.info(
        "auth.jit.groups_updated", user_id=user_id, added_groups=added, removed_groups=removed
    )


def audit_jit_team_assigned(user_id: str, team_name: str, reason: str) -> None:
    """Emit an audit event when a JIT account is assigned to a team.

    Args:
        user_id: Identity of the account being assigned.
        team_name: Name of the team assigned.
        reason: Reason for the assignment (e.g. group mapping rule name).
    """
    logger.info("auth.jit.team_assigned", user_id=user_id, team_name=team_name, reason=reason)


def audit_jit_failed(user_id: str, error: str) -> None:
    """Emit an audit event when JIT provisioning fails.

    Args:
        user_id: Identity for which provisioning failed.
        error: Error description.
    """
    logger.warning("auth.jit.provision_failed", user_id=user_id, error=error)


def audit_resource_change(
    admin_id: str,
    resource_type: str,
    resource_name: str,
    change: str,
    details: dict | None = None,
) -> None:
    """Emit an audit event for a generic resource create/update/delete.

    Args:
        admin_id: Identity of the admin performing the change.
        resource_type: Type of resource (e.g. ``"source"``, ``"fieldmap"``).
        resource_name: Name/identifier of the affected resource.
        change: One of ``"created"``, ``"updated"``, ``"deleted"``.
        details: Optional additional structured details.
    """
    logger.info(
        f"resource.{resource_type}.{change}",
        admin_id=admin_id,
        resource_type=resource_type,
        resource_name=resource_name,
        change=change,
        details=details,
    )
