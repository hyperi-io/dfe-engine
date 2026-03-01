"""Helm values compiler for DFE services.

Merges deployment config + service config + source routing + KEDA wiring
into complete values.yaml per service. Sits ON TOP of Argo CD — generates
what Argo CD consumes, plus handles imperative operations Argo can't do.

Compilation is **pure** (no side effects). Imperative operations (DDL, topics)
are in ``operations.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import SecretStr

from dfe_engine.deployment.registry import DeploymentConfigRegistry
from dfe_engine.helm.environment import EnvironmentConfig
from dfe_engine.helm.models import (
    CompilationResult,
    HelmKedaConfig,
    HelmKedaTrigger,
    HelmServiceValues,
)
from dfe_engine.services.registry import ServiceConfigRegistry
from dfe_engine.source.registry import SourceRegistry
from dfe_engine.yaml_utils import yaml_dump


class HelmValuesCompiler:
    """Compiles Helm values.yaml files from DFE registries + environment.

    Compilation flow per service:
    1. Deployment layer → image, replicas, resources, pod, k8s_service
    2. KEDA wiring → abstract triggers resolved to concrete bootstrap servers
    3. Service config → runtime config dict with source routing injected
    4. Secret scrubbing → blank any SecretStr values
    5. Compose → HelmServiceValues

    Args:
        deployment_registry: Registry for K8s deployment configs.
        service_registry: Registry for service runtime configs.
        source_registry: Registry for Source definitions.
        environment: Target deployment environment.
    """

    def __init__(
        self,
        deployment_registry: DeploymentConfigRegistry,
        service_registry: ServiceConfigRegistry,
        source_registry: SourceRegistry,
        environment: EnvironmentConfig,
    ) -> None:
        self._deploy = deployment_registry
        self._service = service_registry
        self._source = source_registry
        self._env = environment

    def compile_all(
        self,
        group_role_mapping: dict[str, list[str]] | None = None,
        role_permissions: dict[str, set[str]] | None = None,
        argo_project: str = "dfe",
    ) -> CompilationResult:
        """Compile Helm values for all services that have both deployment and service configs.

        Args:
            group_role_mapping: OIDC group → DFE role names (for Argo RBAC generation).
            role_permissions: Role → permitted actions (defaults to built-in).
            argo_project: Argo CD project name for RBAC scoping.

        Returns:
            CompilationResult with helm_values, ddl_statements, kafka_topics,
            argo_rbac_csv, argo_appproject_roles, warnings, and errors.
        """
        result = CompilationResult()
        compiled_services: list[tuple[str, str]] = []

        # Find all deployment configs
        for entry in self._deploy.list_configs():
            service = entry["service"]
            instance = entry["instance"]
            key = f"{service}-{instance}"

            try:
                values = self.compile_service(service, instance)
                result.helm_values[key] = values
                compiled_services.append((service, instance))
            except Exception as e:
                result.errors.append(f"{key}: {e}")

        # Compile DDL and Kafka topics from sources
        result.ddl_statements = self.compile_ddl()
        result.kafka_topics = self.compile_kafka_topics()

        # Generate Argo CD RBAC policies
        from dfe_engine.helm.argo_rbac import (
            generate_appproject_roles,
            generate_rbac_csv,
        )

        result.argo_rbac_csv = generate_rbac_csv(
            group_role_mapping=group_role_mapping,
            role_permissions=role_permissions,
            project=argo_project,
        )
        result.argo_appproject_roles = generate_appproject_roles(
            group_role_mapping=group_role_mapping,
            role_permissions=role_permissions,
            project=argo_project,
        )

        # Generate Argo CD Application + AppProject CRDs
        if self._env.argo.enabled:
            from dfe_engine.helm.argo_app import (
                generate_applications,
                generate_appproject,
            )

            argo = self._env.argo
            result.argo_applications = generate_applications(
                services=compiled_services,
                environment_name=self._env.name,
                namespace=self._env.namespace,
                argo_project=argo.project,
                chart_repo_url=argo.chart_repo_url,
                chart_version=(
                    argo.chart_version or self._env.image_tag_override or "latest"
                ),
                values_path_prefix=argo.values_path_prefix,
                destination_server=argo.destination_server,
                sync_policy=argo.sync_policy.to_argo_dict(),
                chart_overrides=argo.chart_overrides or None,
                extra_labels=argo.labels or None,
                ignore_differences=argo.ignore_differences or None,
            )
            result.argo_appproject = generate_appproject(
                project_name=argo.project,
                environment_name=self._env.name,
                namespace=self._env.namespace,
                source_repos=argo.source_repos,
                destination_server=argo.destination_server,
                roles=result.argo_appproject_roles,
            )

        return result

    def compile_service(self, service: str, instance: str) -> HelmServiceValues:
        """Compile Helm values for a single service instance.

        Args:
            service: Service name (receiver, loader, archiver).
            instance: Deployment instance (default, production, staging).

        Returns:
            HelmServiceValues ready to write as values.yaml.
        """
        # 1. Deployment layer
        deploy = self._deploy.get_config(service, instance)
        deploy_data = deploy.model_dump(mode="json")

        image = deploy.image
        if self._env.image_tag_override:
            image_tag = self._env.image_tag_override
        else:
            image_tag = deploy.image_tag

        resources = deploy_data.get("resources") or {}
        pod = deploy_data.get("pod", {})
        k8s_service = deploy_data.get("service", {})
        hpa = deploy_data.get("hpa", {})
        extra_env = deploy_data.get("extra_env", {})

        # 2. KEDA wiring
        keda = self._compile_keda(deploy.keda)

        # 3. Service config
        config = self._compile_service_config(service, instance)

        # 4. OTEL env var injection
        if self._env.otel.enabled:
            otel = self._env.otel
            extra_env["OTEL_EXPORTER_OTLP_ENDPOINT"] = otel.collector_endpoint
            extra_env["OTEL_EXPORTER_OTLP_PROTOCOL"] = otel.protocol
            extra_env["OTEL_SERVICE_NAME"] = f"dfe-{service}"
            attrs = f"service.namespace=dfe,deployment.environment={self._env.name}"
            for k, v in otel.resource_attributes.items():
                attrs += f",{k}={v}"
            extra_env["OTEL_RESOURCE_ATTRIBUTES"] = attrs

        # 5. Secret refs from environment
        secret_refs = dict(self._env.secret_refs)
        config_secret = deploy_data.get("config_secret", {})
        if config_secret and config_secret.get("name"):
            secret_refs["config-secrets"] = config_secret["name"]

        # 5. Compose
        values = HelmServiceValues(
            image=image,
            image_tag=image_tag,
            replicas=deploy.replicas,
            resources=resources,
            pod=pod,
            k8s_service=k8s_service,
            keda=keda,
            hpa=hpa,
            config=config,
            secret_refs=secret_refs,
            extra_env=extra_env,
        )

        return values

    def compile_ddl(self) -> list[str]:
        """Compile CREATE TABLE DDL for all enabled sources.

        Uses SchemaBuilderV2 to generate DDL from Source definitions.

        Returns:
            List of DDL statement strings.
        """
        statements: list[str] = []
        try:
            from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
            from dfe_engine.source.type_registry import TypeRegistry

            builder = SchemaBuilderV2(type_registry=TypeRegistry())
            for source in self._source.get_all_sources(enabled_only=True):
                try:
                    result = builder.build(source)
                    if result.create_table_ddl:
                        statements.append(result.create_table_ddl)
                except Exception:
                    pass
        except ImportError:
            pass

        return statements

    def compile_kafka_topics(self) -> list[dict[str, Any]]:
        """Compile Kafka topic specs from enabled sources.

        Each source with a ``topic_land`` produces a topic spec.

        Returns:
            List of topic spec dicts with name, partitions, replication_factor.
        """
        topics: list[dict[str, Any]] = []
        seen: set[str] = set()

        for source in self._source.get_all_sources(enabled_only=True):
            topic = source.topic_land
            if topic and topic not in seen:
                seen.add(topic)
                topics.append({
                    "name": topic,
                    "partitions": 3,
                    "replication_factor": 1,
                })

        return topics

    def merge_overrides(
        self,
        values: HelmServiceValues,
        overrides: dict[str, Any],
    ) -> HelmServiceValues:
        """Deep-merge user-provided overrides into compiled Helm values.

        Args:
            values: Compiled Helm values from compile_service().
            overrides: Override dict (e.g. from a per-service override YAML file).

        Returns:
            New HelmServiceValues with overrides applied.
        """
        import copy

        from deepmerge import always_merger

        base = values.model_dump(mode="json")
        merged = copy.deepcopy(base)
        always_merger.merge(merged, overrides)
        return HelmServiceValues.model_validate(merged)

    def write_all(self, result: CompilationResult, output_dir: Path) -> list[Path]:
        """Write compiled Helm values and RBAC policies to files.

        Args:
            result: CompilationResult from compile_all().
            output_dir: Directory to write values files into.

        Returns:
            List of file paths written.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        for key, values in sorted(result.helm_values.items()):
            path = output_dir / f"{key}-values.yaml"
            data = values.model_dump(mode="json")
            yaml_dump(data, path)
            written.append(path)

        # Write Argo CD RBAC policy CSV
        if result.argo_rbac_csv:
            rbac_path = output_dir / "argocd-rbac-policy.csv"
            rbac_path.write_text(result.argo_rbac_csv)
            written.append(rbac_path)

        # Write Argo CD Application CRDs
        if result.argo_applications:
            apps_dir = output_dir / "applications"
            apps_dir.mkdir(parents=True, exist_ok=True)
            for app in result.argo_applications:
                app_name = app["metadata"]["name"]
                path = apps_dir / f"{app_name}.yaml"
                yaml_dump(app, path)
                written.append(path)

        # Write Argo CD AppProject CRD
        if result.argo_appproject:
            name = result.argo_appproject.get("metadata", {}).get("name", "dfe")
            path = output_dir / f"appproject-{name}.yaml"
            yaml_dump(result.argo_appproject, path)
            written.append(path)

        return written

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _compile_keda(self, keda_config) -> HelmKedaConfig:
        """Resolve abstract KEDA config to concrete Helm KEDA config."""
        if not keda_config.enabled:
            return HelmKedaConfig(enabled=False)

        triggers: list[HelmKedaTrigger] = []

        # Kafka trigger
        if keda_config.kafka_trigger:
            kt = keda_config.kafka_trigger
            bootstrap_csv = ",".join(self._env.kafka.bootstrap_servers)
            metadata = {
                "bootstrapServers": bootstrap_csv,
                "consumerGroup": kt.consumer_group,
                "lagThreshold": str(kt.lag_threshold),
            }
            if kt.topic:
                metadata["topic"] = kt.topic

            auth_ref = kt.authentication_ref or self._env.kafka.authentication_ref
            triggers.append(HelmKedaTrigger(
                type="kafka",
                metadata=metadata,
                authentication_ref=auth_ref,
            ))

        # CPU trigger
        if keda_config.cpu_trigger:
            ct = keda_config.cpu_trigger
            triggers.append(HelmKedaTrigger(
                type="cpu",
                metadata={
                    "type": ct.metric_type,
                    "value": str(ct.value),
                },
            ))

        # Prometheus/OTEL metrics trigger
        if keda_config.prometheus_trigger:
            pt = keda_config.prometheus_trigger
            server = pt.server_address
            if not server and self._env.otel.enabled:
                # Default to OTEL Collector's Prometheus endpoint
                host = self._env.otel.collector_endpoint.split("://", 1)[-1]
                host = host.rsplit(":", 1)[0]
                server = f"http://{host}:{self._env.otel.prometheus_port}"
            metadata: dict[str, str] = {
                "serverAddress": server,
                "query": pt.query,
                "threshold": str(pt.threshold),
            }
            if pt.activation_threshold:
                metadata["activationThreshold"] = str(pt.activation_threshold)
            if pt.metric_name:
                metadata["metricName"] = pt.metric_name
            triggers.append(HelmKedaTrigger(
                type="prometheus",
                metadata=metadata,
            ))

        # Generic extra triggers (any KEDA scaler type)
        for gt in getattr(keda_config, "extra_triggers", []):
            triggers.append(HelmKedaTrigger(
                type=gt.type,
                metadata=dict(gt.metadata),
                authentication_ref=gt.authentication_ref,
            ))

        return HelmKedaConfig(
            enabled=True,
            min_replicas=keda_config.min_replicas,
            max_replicas=keda_config.max_replicas,
            polling_interval=keda_config.polling_interval,
            cooldown_period=keda_config.cooldown_period,
            fallback_replicas=keda_config.fallback_replicas,
            triggers=triggers,
        )

    def _compile_service_config(self, service: str, instance: str) -> dict[str, Any]:
        """Get service config dict with environment + source routing injected."""
        from dfe_engine.services.registry import ConfigNotFoundError

        try:
            svc_config = self._service.get_config(service, instance)
        except ConfigNotFoundError:
            return {}

        config = svc_config.model_dump(mode="json")

        # Inject Kafka brokers from environment
        if "kafka" in config:
            config["kafka"]["brokers"] = list(self._env.kafka.bootstrap_servers)

        # Service-specific injections
        if service == "receiver":
            config = self._inject_receiver_routing(config)
        elif service == "loader":
            config = self._inject_loader_routing(config)

        # Scrub secrets
        config = _scrub_secrets(config)

        return config

    def _inject_receiver_routing(self, config: dict[str, Any]) -> dict[str, Any]:
        """Inject source routing into receiver config."""
        from dfe_engine.services.source_routing import compile_receiver_routing

        routing = compile_receiver_routing(self._source)
        config["routing"] = routing.model_dump(mode="json")
        return config

    def _inject_loader_routing(self, config: dict[str, Any]) -> dict[str, Any]:
        """Inject source routing and ClickHouse hosts into loader config."""
        from dfe_engine.services.source_routing import compile_loader_routing

        routing = compile_loader_routing(
            self._source, db=self._env.clickhouse.database
        )
        config["routing"] = routing.model_dump(mode="json")

        # Inject ClickHouse hosts from environment
        if "clickhouse" in config:
            config["clickhouse"]["hosts"] = list(self._env.clickhouse.hosts)
            config["clickhouse"]["database"] = self._env.clickhouse.database
            config["clickhouse"]["username"] = self._env.clickhouse.username

        return config


def _scrub_secrets(data: Any) -> Any:
    """Walk a dict/list and blank any SecretStr values."""
    if isinstance(data, dict):
        return {k: _scrub_secrets(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_scrub_secrets(item) for item in data]
    if isinstance(data, SecretStr):
        return "***"
    return data
