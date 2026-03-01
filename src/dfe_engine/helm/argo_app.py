"""Argo CD Application and AppProject CRD generators.

Generates Kubernetes-ready manifests from DFE compilation state.
Application CRDs are generated per service-instance. One AppProject CRD
is generated per environment, reusing roles from ``argo_rbac.py``.

All functions are pure — they accept data and return dicts. No side effects.
"""

from __future__ import annotations

from typing import Any

_DEFAULT_SYNC_POLICY: dict[str, Any] = {
    "automated": {
        "prune": True,
        "selfHeal": True,
    },
    "syncOptions": ["CreateNamespace=true"],
    "retry": {
        "limit": 5,
        "backoff": {
            "duration": "5s",
            "maxDuration": "3m",
            "factor": 2,
        },
    },
}


def generate_application(
    service: str,
    instance: str,
    environment_name: str,
    namespace: str,
    argo_project: str,
    chart_repo_url: str,
    chart_name: str,
    chart_version: str,
    values_path: str,
    destination_server: str = "https://kubernetes.default.svc",
    sync_policy: dict[str, Any] | None = None,
    extra_labels: dict[str, str] | None = None,
    extra_annotations: dict[str, str] | None = None,
    ignore_differences: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate an Argo CD Application CRD dict.

    Args:
        service: Service name (e.g. "receiver").
        instance: Instance name (e.g. "production").
        environment_name: Environment name (for labels).
        namespace: Target K8s namespace for the workload.
        argo_project: Argo CD project name.
        chart_repo_url: Helm chart repository URL.
        chart_name: Helm chart name.
        chart_version: Helm chart version (targetRevision).
        values_path: Path to values file relative to repo root.
        destination_server: K8s API server URL.
        sync_policy: Override sync policy dict. Defaults to auto-sync
            with prune, selfHeal, and retry.
        extra_labels: Additional labels for the Application metadata.
        extra_annotations: Additional annotations for the Application metadata.
        ignore_differences: Argo CD ignoreDifferences for specific fields.

    Returns:
        Dict representing a complete Application CRD.
    """
    app_name = f"dfe-{service}-{instance}"

    labels: dict[str, str] = {
        "app.kubernetes.io/managed-by": "dfe-engine",
        "dfe.hyperi.io/service": service,
        "dfe.hyperi.io/instance": instance,
        "dfe.hyperi.io/environment": environment_name,
    }
    if extra_labels:
        labels.update(extra_labels)

    metadata: dict[str, Any] = {
        "name": app_name,
        "namespace": "argocd",
        "labels": labels,
        "finalizers": ["resources-finalizer.argocd.argoproj.io"],
    }
    if extra_annotations:
        metadata["annotations"] = extra_annotations

    if sync_policy is None:
        sync_policy = _DEFAULT_SYNC_POLICY

    app: dict[str, Any] = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": metadata,
        "spec": {
            "project": argo_project,
            "source": {
                "repoURL": chart_repo_url,
                "chart": chart_name,
                "targetRevision": chart_version,
                "helm": {
                    "valueFiles": [values_path],
                },
            },
            "destination": {
                "server": destination_server,
                "namespace": namespace,
            },
            "syncPolicy": sync_policy,
        },
    }

    if ignore_differences:
        app["spec"]["ignoreDifferences"] = ignore_differences

    return app


def generate_applications(
    services: list[tuple[str, str]],
    environment_name: str,
    namespace: str,
    argo_project: str,
    chart_repo_url: str,
    chart_version: str,
    values_path_prefix: str = "values",
    destination_server: str = "https://kubernetes.default.svc",
    sync_policy: dict[str, Any] | None = None,
    chart_overrides: dict[str, str] | None = None,
    extra_labels: dict[str, str] | None = None,
    ignore_differences: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Generate Application CRDs for all compiled services.

    Chart name derived by convention: ``dfe-{service}``. Override via
    ``chart_overrides`` dict keyed by service name.

    Args:
        services: List of (service, instance) tuples.
        environment_name: Environment name.
        namespace: Target K8s namespace.
        argo_project: Argo CD project name.
        chart_repo_url: Helm chart repository URL.
        chart_version: Default chart version for all services.
        values_path_prefix: Directory prefix for values files (default: "values").
        destination_server: K8s API server URL.
        sync_policy: Override sync policy for all Applications.
        chart_overrides: Service name → chart name overrides.
        extra_labels: Additional labels for all Application CRDs.
        ignore_differences: Argo CD ignoreDifferences for all Applications.

    Returns:
        List of Application CRD dicts, sorted by app name.
    """
    overrides = chart_overrides or {}
    applications: list[dict[str, Any]] = []

    for service, instance in sorted(services):
        chart_name = overrides.get(service, f"dfe-{service}")
        key = f"{service}-{instance}"
        values_path = f"{values_path_prefix}/{key}-values.yaml"

        app = generate_application(
            service=service,
            instance=instance,
            environment_name=environment_name,
            namespace=namespace,
            argo_project=argo_project,
            chart_repo_url=chart_repo_url,
            chart_name=chart_name,
            chart_version=chart_version,
            values_path=values_path,
            destination_server=destination_server,
            sync_policy=sync_policy,
            extra_labels=extra_labels,
            ignore_differences=ignore_differences,
        )
        applications.append(app)

    return applications


def generate_appproject(
    project_name: str,
    environment_name: str,
    namespace: str,
    source_repos: list[str],
    destination_server: str = "https://kubernetes.default.svc",
    roles: list[dict[str, Any]] | None = None,
    cluster_resource_whitelist: list[dict[str, str]] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Generate an Argo CD AppProject CRD dict.

    Args:
        project_name: Argo CD project name (e.g. "dfe").
        environment_name: Environment name (for description).
        namespace: Allowed destination namespace.
        source_repos: Allowed source repository URLs.
        destination_server: Allowed destination K8s API server.
        roles: AppProject ``.spec.roles`` (from ``generate_appproject_roles()``).
        cluster_resource_whitelist: Allowed cluster-scoped resources.
        description: Project description.

    Returns:
        Dict representing a complete AppProject CRD.
    """
    spec: dict[str, Any] = {
        "description": description or f"DFE Engine - {environment_name}",
        "sourceRepos": source_repos,
        "destinations": [
            {
                "server": destination_server,
                "namespace": namespace,
            },
        ],
    }

    if roles:
        spec["roles"] = roles

    if cluster_resource_whitelist:
        spec["clusterResourceWhitelist"] = cluster_resource_whitelist

    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "AppProject",
        "metadata": {
            "name": project_name,
            "namespace": "argocd",
        },
        "spec": spec,
    }
