"""Deployment contract for dfe-engine itself.

Mirrors the pattern dfe-loader uses (Rust): the app exports a single
:class:`hyperi_pylib.deployment.DeploymentContract` describing its
deployment-facing surface (image, ports, health probes, secrets, OCI labels).
pylib's Python-native generators consume that contract to emit:

- ``Dockerfile`` -- the full multi-stage image (uv builder stage + runtime
  stage copying the ``/app/.venv``). pylib derives the base image from
  ``python_version``; no hand-written Dockerfile to keep in sync.
- ``container-manifest.json`` -- JSON describing OCI labels and contract points
  for hyperi-ci's container build pipeline
- ``argocd-application.yaml`` -- ArgoCD Application CR pointing at the chart

The :func:`engine_deployment_contract` factory is the single source of truth.
``DfeApiApp.deployment_contract()`` returns it so ``dfe-api generate-artefacts``
emits the artefacts, and pylib's ``validate_dockerfile`` / ``validate_helm_values``
drift checks (see ``tests/unit/test_deployment/test_contract.py``) keep the
committed ``Dockerfile`` and ``chart/`` aligned with it.
"""

from __future__ import annotations

from hyperi_pylib.deployment import (
    DeploymentContract,
    HealthContract,
    ImageProfile,
    OciLabels,
    SecretEnvContract,
    SecretGroupContract,
    image_registry_from_cascade,
)


def engine_deployment_contract() -> DeploymentContract:
    """Build the deployment contract for dfe-engine.

    Defaults match the committed ``Dockerfile`` and ``chart/values.yaml``.
    ``image_registry`` reads from the config cascade
    (``deployment.image_registry``) so ops can override per-environment without
    touching code; the base image is derived by pylib from ``python_version``.
    """
    return DeploymentContract(
        app_name="dfe-engine",
        binary_name="dfe-api",
        description="DFE Engine -- REST API and config control plane for the Data Fusion Engine",
        metrics_port=8000,
        health=HealthContract(
            liveness_path="/api/v1/system/health",
            readiness_path="/api/v1/system/health",
            metrics_path="/metrics",
        ),
        env_prefix="DFE",
        metric_prefix="engine",
        config_mount_path="/etc/dfe/config",
        image_registry=image_registry_from_cascade(),
        python_version="3.12",
        entrypoint_args=["run"],
        secrets=[
            SecretGroupContract(
                group_name="clickhouse",
                env_vars=[
                    SecretEnvContract(
                        env_var="DFE__CLICKHOUSE__PASSWORD",
                        key_name="password",
                        secret_key="clickhouse-password",  # noqa: S106 -- K8s Secret data key, not a credential
                    ),
                ],
            ),
            SecretGroupContract(
                group_name="jwt",
                env_vars=[
                    SecretEnvContract(
                        env_var="DFE__AUTH__JWT_SECRET",
                        key_name="secret",
                        secret_key="jwt-secret",  # noqa: S106 -- K8s Secret data key, not a credential
                    ),
                ],
            ),
        ],
        depends_on=["clickhouse"],
        # KEDA not used for dfe-engine (control plane -- HPA on CPU is sufficient).
        keda=None,
        image_profile=ImageProfile.PRODUCTION,
        oci_labels=OciLabels(
            title="dfe-engine",
            description="DFE Engine -- REST API and config control plane",
        ),
    )


__all__ = ["engine_deployment_contract"]
