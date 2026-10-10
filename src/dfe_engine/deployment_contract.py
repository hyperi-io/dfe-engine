"""Deployment contract for dfe-engine itself.

Mirrors the pattern dfe-loader uses (Rust): the app exports a single
:class:`scalo.deployment.DeploymentContract` describing its
deployment-facing surface (image, ports, health probes, secrets, writable
paths, resources, OCI labels). Two consumers read it:

- ``dfe-engine generate-artefacts`` writes ``deployment-contract.json``, the
  ``Dockerfile`` runtime stage and ``container-manifest.json``. hyperi-ci's
  Build job runs it, and the release assembles the thin Helm chart from that
  contract on the scalo-service library chart (``release.helm`` in
  ``.hyperi-ci.yaml``), so every field below reaches the deployed pod.
- scalo's ``validate_dockerfile`` drift check (see
  ``tests/unit/test_deployment/test_contract.py``) keeps the committed
  ``Dockerfile`` aligned with it. The published chart is assembled from the
  contract, so there is no committed chart to keep aligned.

The :func:`engine_deployment_contract` factory is the single source of truth.
``DfeEngineApp.deployment_contract()`` returns it.
"""

import os

from scalo.deployment import (
    DeploymentContract,
    HealthContract,
    ImageProfile,
    OciLabels,
    PortContract,
    ResourceList,
    ResourcesContract,
    SecretEnvContract,
    SecretGroupContract,
    WritablePath,
)

# scalo requires an explicit registry, so default to the one dfe-engine publishes to; ops override via env.
_DEFAULT_IMAGE_REGISTRY = "ghcr.io/hyperi-io"


def _env(env_var: str, key_name: str, secret_key: str) -> SecretEnvContract:
    """Map one env var the engine reads to a key of its group's Kubernetes Secret.

    Args:
        env_var: The variable ``dfe_engine.settings`` reads.
        key_name: The key under the group in the chart's values.
        secret_key: The default data key in the group's Secret.

    Returns:
        The contract entry for the variable.
    """
    return SecretEnvContract(env_var=env_var, key_name=key_name, secret_key=secret_key)


def engine_deployment_contract() -> DeploymentContract:
    """Build the deployment contract for dfe-engine.

    ``image_registry`` is ``DFE_DEPLOYMENT_IMAGE_REGISTRY`` when that env var is
    set, else ``_DEFAULT_IMAGE_REGISTRY``; no config file sets it. The base image
    is derived by scalo from ``python_version``.

    Returns:
        The engine's contract at schema version 4.
    """
    return DeploymentContract(
        app_name="dfe-engine",
        binary_name="dfe-engine",
        description="DFE Engine -- REST API and config control plane for the Data Fusion Engine",
        # Observability port (#106). scalo's ServiceApp binds this and
        # serves health + /metrics on it, SEPARATE from the API traffic port -- so
        # the unauthenticated /metrics is no longer on the public 8000. API traffic
        # is the `http` extra port below; the ingress targets that.
        metrics_port=9090,
        extra_ports=[PortContract(name="http", port=8000)],
        health=HealthContract(
            # /livez + /readyz are the WHOLE probe surface scalo's observability
            # server serves, on the 9090 port rather than the 8000 API port
            # (#106). The aliases these used to be (/health/live, /health/ready)
            # were retired in scalo 2.29.12: they 404 now, so a probe still
            # aimed at one fails liveness and crashloops a healthy pod.
            liveness_path="/livez",
            readiness_path="/readyz",
            metrics_path="/metrics",
            startup_budget_seconds=300,
        ),
        env_prefix="DFE",
        metric_prefix="dfe",
        # Empty, so the chart mounts no config file: the YAML SSoT lives in the `config` writable path.
        config_mount_path="",
        image_registry=os.environ.get("DFE_DEPLOYMENT_IMAGE_REGISTRY") or _DEFAULT_IMAGE_REGISTRY,
        python_version="3.14",
        # Digest-pinned runtime base (#106). python:3.14-slim is already
        # Debian 13 trixie; pinned so the tag cannot float. The committed
        # Dockerfile's runtime FROM must match this literal (validate_dockerfile
        # substring check). Re-resolve on a bump; Renovate maintains it.
        base_image="python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2",
        entrypoint_args=["run"],
        # Env names are the ones dfe_engine.settings reads, single-underscore under the DFE_ prefix.
        secrets=[
            SecretGroupContract(
                group_name="clickhouse",
                env_vars=[_env("DFE_CLICKHOUSE_PASSWORD", "password", "password")],
            ),
            SecretGroupContract(
                group_name="jwt",
                env_vars=[_env("DFE_API_JWT_SECRET", "secret", "jwt-secret")],
            ),
            SecretGroupContract(
                group_name="admin",
                env_vars=[_env("DFE_AUTH_LOCAL_ADMIN_PASSWORD", "password", "admin-password")],
            ),
            SecretGroupContract(
                group_name="breakglass",
                env_vars=[_env("DFE_AUTH_BREAKGLASS_PASSWORD", "password", "breakglass-password")],
            ),
            SecretGroupContract(
                group_name="hunt-runner",
                env_vars=[_env("DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD", "password", "password")],
            ),
            SecretGroupContract(
                group_name="kafka",
                env_vars=[
                    _env("DFE_KAFKA_SASL_MECHANISM", "mechanism", "sasl.mechanism"),
                    _env("DFE_KAFKA_SASL_USERNAME", "username", "username"),
                    _env("DFE_KAFKA_SASL_PASSWORD", "password", "password"),
                ],
            ),
            SecretGroupContract(
                group_name="gitops",
                env_vars=[
                    _env("DFE_GITOPS_USERNAME", "username", "username"),
                    _env("DFE_GITOPS_TOKEN", "token", "password"),
                ],
            ),
            # Optional: an engine with no seed accounts starts with the variable unset.
            SecretGroupContract(
                group_name="seed-accounts",
                env_vars=[_env("DFE_AUTH_LOCAL_SEED_ACCOUNTS", "accounts", "seed-accounts")],
                optional=True,
            ),
        ],
        depends_on=["clickhouse"],
        # KEDA not used for dfe-engine (control plane -- HPA on CPU is sufficient).
        keda=None,
        # The YAML SSoT directory DFE_CONFIG_DIR names, on a claim so the config outlives the pod.
        writable_paths=[WritablePath(name="config", path="/config", persistent=True, size="1Gi")],
        termination_grace_seconds=30,
        resources=ResourcesContract(
            requests=ResourceList(cpu="200m", memory="256Mi"),
            limits=ResourceList(cpu="1", memory="1Gi"),
        ),
        # One pod: the config claim is ReadWriteOnce, and two replicas serve two source registries (#361).
        singleton=True,
        image_profile=ImageProfile.PRODUCTION,
        # scalo ships no vendor, licence or copyright defaults, so the product identity is set here.
        oci_labels=OciLabels(
            title="dfe-engine",
            description="DFE Engine -- REST API and config control plane",
            vendor="HYPERI PTY LIMITED",
            label_namespace="io.hyperi",
            licenses="BUSL-1.1",
            copyright="(c) 2026 HYPERI PTY LIMITED",
        ),
    )


__all__ = ["engine_deployment_contract"]
