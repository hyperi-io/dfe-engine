"""Deployment contract for dfe-engine itself.

Mirrors the pattern dfe-loader uses (Rust): the app exports a single
:class:`scalo.deployment.DeploymentContract` describing its
deployment-facing surface (image, ports, health probes, secrets, OCI labels).
pylib's Python-native generators consume that contract to emit:

- ``Dockerfile`` -- the full multi-stage image (uv builder stage + runtime
  stage copying the ``/app/.venv``). pylib derives the base image from
  ``python_version``; no hand-written Dockerfile to keep in sync.
- ``container-manifest.json`` -- JSON describing OCI labels and contract points
  for hyperi-ci's container build pipeline
- ``argocd-application.yaml`` -- ArgoCD Application CR pointing at the chart

The :func:`engine_deployment_contract` factory is the single source of truth.
``DfeEngineApp.deployment_contract()`` returns it so ``dfe-engine generate-artefacts``
emits the artefacts, and pylib's ``validate_dockerfile`` / ``validate_helm_values``
drift checks (see ``tests/unit/test_deployment/test_contract.py``) keep the
committed ``Dockerfile`` and ``chart/`` aligned with it.
"""

from __future__ import annotations

import os

from scalo.deployment import (
    DeploymentContract,
    HealthContract,
    ImageProfile,
    OciLabels,
    PortContract,
    SecretEnvContract,
    SecretGroupContract,
)

# dfe identity default; scalo's cascade helper neutralises to localhost:5000, so
# default to dfe's registry here and let ops override via env (#60).
_DEFAULT_IMAGE_REGISTRY = "ghcr.io/hyperi-io"


def engine_deployment_contract() -> DeploymentContract:
    """Build the deployment contract for dfe-engine.

    Defaults match the committed ``Dockerfile`` and ``chart/values.yaml``.
    ``image_registry`` reads from the config cascade
    (``deployment.image_registry``) so ops can override per-environment without
    touching code; the base image is derived by pylib from ``python_version``.
    """
    return DeploymentContract(
        app_name="dfe-engine",
        binary_name="dfe-engine",
        description="DFE Engine -- REST API and config control plane for the Data Fusion Engine",
        # Observability port (#106 P2.4 / P1.3). scalo's ServiceApp binds this and
        # serves health + /metrics on it, SEPARATE from the API traffic port -- so
        # the unauthenticated /metrics is no longer on the public 8000. API traffic
        # is the `http` extra port below; the ingress targets that.
        metrics_port=9090,
        extra_ports=[PortContract(name="http", port=8000)],
        health=HealthContract(
            # The canonical pair, served on the 9090 observability port rather
            # than the 8000 API port. There are no aliases: scalo dropped every
            # other spelling, and a retired path now 404s.
            liveness_path="/livez",
            readiness_path="/readyz",
            metrics_path="/metrics",
        ),
        env_prefix="DFE",
        metric_prefix="dfe",
        config_mount_path="/etc/dfe/config",
        image_registry=os.environ.get("DFE_DEPLOYMENT_IMAGE_REGISTRY") or _DEFAULT_IMAGE_REGISTRY,
        python_version="3.12",
        # Digest-pinned runtime base (#106 P2.3). python:3.12-slim is already
        # Debian 13 trixie; pinned so the tag cannot float. The committed
        # Dockerfile's runtime FROM must match this literal (validate_dockerfile
        # substring check). Re-resolve on a bump; Renovate maintains it.
        base_image="python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de",
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
            # Explicit: scalo's DEFAULT_VENDOR is being neutralised; keep dfe identity (#60).
            vendor="HyperI",
        ),
    )


__all__ = ["engine_deployment_contract"]
