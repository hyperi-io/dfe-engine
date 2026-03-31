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
