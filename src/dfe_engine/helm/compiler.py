"""Helm values compiler for DFE services.

Merges deployment config + service config + source routing + KEDA wiring
into complete values.yaml per service. Sits ON TOP of Argo CD -- generates
what Argo CD consumes, plus handles imperative operations Argo can't do.

Compilation is **pure** (no side effects). Imperative operations (DDL, topics)
are in ``operations.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import SecretStr
from scalo.logger import logger

from dfe_engine.auth.roles import RoleConfig
from dfe_engine.deployment.registry import DeploymentConfigRegistry
from dfe_engine.helm.environment import EnvironmentConfig, ExternalComponent
from dfe_engine.helm.models import (
    CompilationResult,
    HelmDeployMeta,
    HelmImage,
    HelmKeda,
    HelmKedaTrigger,
    HelmServiceValues,
)
from dfe_engine.services.registry import ServiceConfigRegistry
from dfe_engine.source.registry import SourceRegistry
from dfe_engine.yaml_utils import yaml_dump, yaml_load


class HelmValuesCompiler:
    """Compiles Helm values.yaml files from DFE registries + environment.

    Compilation flow per service:
    1. Deployment layer -> image, replicas, resources, pod, k8s_service
    2. KEDA wiring -> abstract triggers resolved to concrete bootstrap servers
    3. Service config -> runtime config dict with source routing injected
    4. Secret scrubbing -> blank any SecretStr values
    5. Compose -> HelmServiceValues

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
        role_config: RoleConfig | None = None,
        argo_project: str = "dfe",
    ) -> CompilationResult:
        """Compile Helm values for all services that have both deployment and service configs.

        Args:
            group_role_mapping: OIDC group -> DFE role names (for Argo RBAC generation).
            role_config: Role configuration (defaults to built-in roles.yaml).
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
            role_config=role_config,
            project=argo_project,
        )
        result.argo_appproject_roles = generate_appproject_roles(
            group_role_mapping=group_role_mapping,
            role_config=role_config,
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
                chart_version=(argo.chart_version or self._env.image_tag_override or "latest"),
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

        # Compile external (Mode 2) components
        for component in self._env.components:
            if not component.enabled:
                continue
            comp_values = self.compile_external_component(component)
            for inst_key, values_dict in comp_values.items():
                result.helm_values[inst_key] = values_dict
            # Generate Argo CD Application CRDs for external components
            if self._env.argo.enabled:
                comp_apps = self._compile_external_argo_apps(component)
                result.argo_applications.extend(comp_apps)

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
        # service config + hpa are left to the chart (its defaults stand; KEDA
        # replaces HPA). The engine overlay only carries the dials it owns.
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

        # 6. Node placement: map the deploy config's pod nodeSelector/tolerations
        # to the chart's nodeScheduling key (dfe-common.scheduling reads it).
        node_scheduling: dict[str, Any] = {}
        if pod.get("nodeSelector"):
            node_scheduling["nodeSelector"] = pod["nodeSelector"]
        if pod.get("tolerations"):
            node_scheduling["tolerations"] = pod["tolerations"]

        # 7. Compose -- chart-shaped overlay. image.repository carries the full
        # repo (deploy.image); the chart's dfe-common.image uses it directly.
        return HelmServiceValues(
            deploy=HelmDeployMeta(service=f"dfe-{service}", instance=instance),
            image=HelmImage(
                repository=image,
                tag=image_tag,
                pullPolicy=pod.get("imagePullPolicy", "IfNotPresent"),
            ),
            replicaCount=deploy.replicas,
            resources=resources,
            keda=keda,
            nodeScheduling=node_scheduling,
            config=config,
            secret_refs=secret_refs,
            extra_env=extra_env,
        )

    def compile_external_component(self, component: ExternalComponent) -> dict[str, dict[str, Any]]:
        """Compile Helm values for an external (Mode 2) component.

        Loads base values from each instance's values file path, then
        deep-merges any ``values_overrides`` for that instance on top.

        Args:
            component: ExternalComponent model from EnvironmentConfig.

        Returns:
            Dict mapping ``{component.name}-{instance}`` to merged values dict.
        """
        import copy

        from dfe_engine.yaml_utils import deep_merge

        results: dict[str, dict[str, Any]] = {}
        for inst_name, values_path in component.instances.items():
            key = f"{component.name}-{inst_name}"
            try:
                base = yaml_load(values_path) or {}
            except FileNotFoundError:
                base = {}

            overrides = component.values_overrides.get(inst_name, {})
            if overrides:
                merged = copy.deepcopy(base)
                deep_merge(merged, overrides)
            else:
                merged = base

            results[key] = merged
        return results

    def _compile_external_argo_apps(self, component: ExternalComponent) -> list[dict[str, Any]]:
        """Generate Argo CD Application CRDs for an external component."""
        from dfe_engine.helm.argo_app import generate_application

        apps: list[dict[str, Any]] = []
        argo = self._env.argo
        for inst_name in component.instances:
            app_name = f"{component.name}-{inst_name}"
            values_path = f"{argo.values_path_prefix}/{app_name}-values.yaml"
            app = generate_application(
                service=component.name,
                instance=inst_name,
                environment_name=self._env.name,
                namespace=component.namespace,
                argo_project=argo.project,
                chart_repo_url=component.chart.repo_url,
                chart_name=component.chart.name,
                chart_version=component.chart.version or "latest",
                values_path=values_path,
                destination_server=argo.destination_server,
                sync_policy=argo.sync_policy.to_argo_dict(),
                extra_labels=argo.labels or None,
            )
            apps.append(app)
        return apps

    def compile_ddl(self) -> list[str]:
        """Compile CREATE TABLE DDL for active AND dormant sources.

        Dormant sources keep their schema pre-positioned (creates are
        idempotent), so enabling one later needs no schema step - only
        disabled sources are excluded (their tables are reclaimed).

        Returns:
            List of DDL statement strings.
        """
        statements: list[str] = []
        try:
            from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
            from dfe_engine.settings import get_settings
            from dfe_engine.source.type_registry import TypeRegistry

            ch = get_settings().clickhouse
            builder = SchemaBuilderV2(
                registry=TypeRegistry.default(),
                default_engine=ch.default_engine,
                default_ttl_days=ch.default_ttl_days,
            )
            for source in self._source.get_all_sources(states=("active", "dormant")):
                try:
                    result = builder.build(source)
                    if result.create_table_ddl:
                        statements.append(result.create_table_ddl)
                except Exception as exc:
                    # One source that cannot build must not take the rest with it,
                    # but a table silently missing from the compile is worse.
                    logger.warning(
                        "no DDL compiled for this source", source=source.source, error=str(exc)
                    )
        except ImportError:
            pass

        return statements

    def compile_kafka_topics(self) -> list[dict[str, Any]]:
        """Compile Kafka topic specs from ACTIVE sources only.

        Each source yields ``_land``; a source with a transform also yields
        ``_load``, because the transform writes there and the loader reads it.
        Dormant sources have no live pipeline, so they get no topic.

        Partitions and replication factor come from Kafka settings - a
        single-broker dev cluster and a real one need different numbers, and
        baking either in makes the topic wrong on the other.

        Returns:
            List of topic spec dicts with name, partitions, replication_factor.
        """
        from dfe_engine.kafka.topics import source_topic_specs
        from dfe_engine.settings import get_settings

        ks = get_settings().kafka
        topics: list[dict[str, Any]] = []
        seen: set[str] = set()

        for source in self._source.get_all_sources(states=("active",)):
            for spec in source_topic_specs(
                source,
                partitions=ks.topic_partitions,
                replication_factor=ks.topic_replication_factor,
            ):
                if spec.name and spec.name not in seen:
                    seen.add(spec.name)
                    topics.append(spec.to_dict())

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

        from dfe_engine.yaml_utils import deep_merge

        base = values.model_dump(mode="json")
        merged = copy.deepcopy(base)
        deep_merge(merged, overrides)
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
            if hasattr(values, "model_dump"):
                data = values.model_dump(mode="json")
            else:
                data = values  # External component -- already a dict
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

    def _compile_keda(self, keda_config) -> HelmKeda:
        """Resolve abstract KEDA config to the chart's ``keda`` shape.

        Emits the scaling BOUNDS (enabled/min/max/cooldown/polling) always. It
        leaves ``triggers`` as None UNLESS the deploy config explicitly configures
        triggers -- so by default the chart's own triggers stand: CPU, plus the
        gated ScalingPressure metrics-api trigger where ``keda.pressure.enabled``.
        DFE scales on those, not raw consumer-group lag, which also rises when a
        downstream stage is broken. Helm replaces lists, so any list emitted here,
        an explicit ``kafka_trigger`` included, replaces the chart's triggers, CPU too.
        """
        if not keda_config.enabled:
            return HelmKeda(enabled=False)

        triggers: list[HelmKedaTrigger] = []

        if keda_config.kafka_trigger:
            kt = keda_config.kafka_trigger
            metadata = {
                "bootstrapServers": ",".join(self._env.kafka.bootstrap_servers),
                "consumerGroup": kt.consumer_group,
                "lagThreshold": str(kt.lag_threshold),
            }
            if kt.topic:
                metadata["topic"] = kt.topic
            auth_ref = kt.authentication_ref or self._env.kafka.authentication_ref
            triggers.append(
                HelmKedaTrigger(
                    type="kafka",
                    metadata=metadata,
                    authenticationRef={"name": auth_ref} if auth_ref else None,
                )
            )

        if keda_config.cpu_trigger:
            ct = keda_config.cpu_trigger
            triggers.append(
                HelmKedaTrigger(
                    type="cpu",
                    metadata={"type": ct.metric_type, "value": str(ct.value)},
                )
            )

        if keda_config.prometheus_trigger:
            pt = keda_config.prometheus_trigger
            server = pt.server_address
            if not server and self._env.otel.enabled:
                host = self._env.otel.collector_endpoint.split("://", 1)[-1].rsplit(":", 1)[0]
                server = f"http://{host}:{self._env.otel.prometheus_port}"
            metadata = {
                "serverAddress": server,
                "query": pt.query,
                "threshold": str(pt.threshold),
            }
            if pt.activation_threshold:
                metadata["activationThreshold"] = str(pt.activation_threshold)
            if pt.metric_name:
                metadata["metricName"] = pt.metric_name
            triggers.append(HelmKedaTrigger(type="prometheus", metadata=metadata))

        for gt in getattr(keda_config, "extra_triggers", []):
            triggers.append(
                HelmKedaTrigger(
                    type=gt.type,
                    metadata=dict(gt.metadata),
                    authenticationRef={"name": gt.authentication_ref}
                    if gt.authentication_ref
                    else None,
                )
            )

        return HelmKeda(
            enabled=True,
            minReplicaCount=keda_config.min_replicas,
            maxReplicaCount=keda_config.max_replicas,
            pollingInterval=keda_config.polling_interval,
            cooldownPeriod=keda_config.cooldown_period,
            triggers=triggers or None,
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

        # The environment names one database, and it is the one DFE tables live in.
        data_db = self._env.clickhouse.database
        routing = compile_loader_routing(self._source, db=data_db)
        # Only the keys the sources derive are stamped; a model default emitted
        # here would overwrite the deployment's own setting for the same key.
        config["routing"] = {
            **(config.get("routing") or {}),
            **routing.model_dump(mode="json", exclude_unset=True),
        }

        # Inject ClickHouse hosts from environment
        if "clickhouse" in config:
            config["clickhouse"]["hosts"] = list(self._env.clickhouse.hosts)
            config["clickhouse"]["database"] = data_db
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
