#  Project:      dfe-engine
#  File:         auth/deployment_hints.py
#  Purpose:      Deploy kind plus the per-kind credential fetch and rotate commands
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What kind of deployment this is, and how the operator gets the admin password.

The deploy mints the admin password into its own secret store, so the login page
has to tell the operator where to read it from -- the Rancher move. The command
differs per deployment vehicle and nothing else in the engine knows it, so it is
derived here and served on ``/auth/setup-status`` while setup is incomplete.

``deployment.target`` is injected by the deployer and wins outright. Nothing
injects it on a bare ``dfe-engine`` run, so the fallback is scalo's own runtime
detection -- the same cascade that picks the container paths.
"""

from __future__ import annotations

from scalo.runtime import RuntimeEnvironment

DOCKER = "docker"
KUBERNETES = "kubernetes"
LOCAL = "local"

# scalo detection methods that mean kubernetes rather than plain docker.
_K8S_METHODS = frozenset({"k8s_serviceaccount", "kubernetes"})

# The env var every deployment fills with the admin password.
ADMIN_PASSWORD_ENV = "DFE_AUTH_LOCAL_ADMIN_PASSWORD"


def detect_deploy_kind(target: str = "") -> str:
    """docker | kubernetes | local. Injected target wins; else scalo's detection."""
    if target in (DOCKER, KUBERNETES):
        return target
    paths = RuntimeEnvironment("dfe-engine").detect_runtime()
    if not paths.is_container:
        return LOCAL
    return KUBERNETES if paths.detection_method in _K8S_METHODS else DOCKER


def credential_fetch_command(
    kind: str,
    *,
    namespace: str = "",
    secret_name: str = "",
    secret_key: str = "",
) -> str:
    """The one-line command that prints this deployment's admin password."""
    if kind == DOCKER:
        return "make creds"
    if kind == KUBERNETES:
        ns = namespace or "<namespace>"
        return (
            f"kubectl -n {ns} get secret {secret_name} "
            f"-o jsonpath='{{.data.{secret_key}}}' | base64 -d"
        )
    return f"read {ADMIN_PASSWORD_ENV} from the engine's environment file (.env)"


def rotation_store_command(
    kind: str,
    *,
    namespace: str = "",
    secret_name: str = "",
    secret_key: str = "",
) -> str:
    """The store operation that rotates the admin password, for the 501 body.

    The engine never writes the password into its own YAML: the store that injects
    it is the source, so a rotation the engine performed alone would be reverted by
    the next boot reconcile.
    """
    if kind == DOCKER:
        return f"set {ADMIN_PASSWORD_ENV} in .env, then: make up"
    if kind == KUBERNETES:
        ns = namespace or "<namespace>"
        return (
            f"kubectl -n {ns} patch secret {secret_name} --type merge "
            f'-p \'{{"stringData":{{"{secret_key}":"<new-password>"}}}}\' '
            "and restart the engine pods"
        )
    return f"set {ADMIN_PASSWORD_ENV} in the engine's environment file and restart it"
