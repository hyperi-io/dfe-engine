"""Unit tests for dfe-engine's deployment contract.

Verifies that :func:`dfe_engine.deployment_contract.engine_deployment_contract`
is well-formed and that scalo's generators emit valid Dockerfile-runtime,
ArgoCD Application, container manifest, and Helm chart artefacts from it.

These tests are the dfe-engine analogue of dfe-loader's
``tests/integration/helm_contract.rs`` + ``tests/integration/deployment.rs``:
they catch drift between the contract and committed artefacts before CI does.
"""

import json
from pathlib import Path

import pytest
import yaml

from dfe_engine.deployment_contract import engine_deployment_contract

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Where _get_env_overrides() puts each Secret-fed variable the contract names.
_OVERRIDE_PATHS = {
    "DFE_CLICKHOUSE_PASSWORD": ("clickhouse", "password"),
    "DFE_API_JWT_SECRET": ("api", "jwt_secret"),
    "DFE_AUTH_LOCAL_ADMIN_PASSWORD": ("auth", "local", "admin_password"),
    "DFE_AUTH_BREAKGLASS_PASSWORD": ("auth", "local", "breakglass_password"),
    "DFE_KAFKA_SASL_MECHANISM": ("kafka", "sasl_mechanism"),
    "DFE_KAFKA_SASL_USERNAME": ("kafka", "sasl_username"),
    "DFE_KAFKA_SASL_PASSWORD": ("kafka", "sasl_password"),
    "DFE_GITOPS_USERNAME": ("gitops", "username"),
    "DFE_GITOPS_TOKEN": ("gitops", "token"),
}
_SEED_ACCOUNTS = "DFE_AUTH_LOCAL_SEED_ACCOUNTS"
_HUNT_RUNNER_PASSWORD = "DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD"


def _secret_env_vars() -> list[str]:
    return [env.env_var for group in engine_deployment_contract().secrets for env in group.env_vars]


# ---------------------------------------------------------------------------
# Contract well-formedness
# ---------------------------------------------------------------------------


class TestContractWellFormed:
    """The contract instantiates and exposes the expected shape."""

    def test_contract_instantiates(self) -> None:
        contract = engine_deployment_contract()
        assert contract.app_name == "dfe-engine"
        assert contract.binary_name == "dfe-engine"
        # Observability port (#106). API traffic moved to the `http` extra
        # port; scalo serves health + /metrics on 9090, off the public 8000.
        assert contract.metrics_port == 9090
        assert [(p.name, p.port) for p in contract.extra_ports] == [("http", 8000)]
        assert contract.env_prefix == "DFE"
        # No config file: a non-empty path makes the chart render and mount a dfe-engine-config ConfigMap.
        assert contract.config_mount_path == ""

    def test_contract_is_the_schema_the_library_renders(self) -> None:
        # scalo below 2.31.3 writes schema 3, which the scalo-service library refuses to assemble.
        assert engine_deployment_contract().schema_version == 4

    def test_config_directory_is_a_claim(self) -> None:
        contract = engine_deployment_contract()
        [config] = contract.writable_paths
        assert (config.name, config.path, config.persistent) == ("config", "/config", True)
        # The claim is ReadWriteOnce, so only one pod may hold it.
        assert contract.singleton is True

    def test_contract_health_paths(self) -> None:
        contract = engine_deployment_contract()
        # scalo's health-router paths, which the app serves; /api/v1/system/health
        # 404s (#106). /livez + /readyz are the whole surface -- the
        # /health/live|ready aliases were retired in scalo 2.29.12, so a contract
        # still naming one generates a chart probe that 404s.
        assert contract.health.liveness_path == "/livez"
        assert contract.health.readiness_path == "/readyz"
        assert contract.health.metrics_path == "/metrics"

    def test_contract_secrets(self) -> None:
        contract = engine_deployment_contract()
        assert [g.group_name for g in contract.secrets] == [
            "clickhouse",
            "jwt",
            "admin",
            "breakglass",
            "hunt-runner",
            "kafka",
            "gitops",
            "seed-accounts",
        ]
        assert [g.group_name for g in contract.secrets if g.optional] == ["seed-accounts"]

    def test_every_secret_env_var_has_a_reader(self) -> None:
        expected = set(_OVERRIDE_PATHS) | {_SEED_ACCOUNTS, _HUNT_RUNNER_PASSWORD}
        assert set(_secret_env_vars()) == expected

    @pytest.mark.parametrize("env_var", sorted(_OVERRIDE_PATHS))
    def test_secret_env_var_reaches_the_settings(
        self, env_var: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dfe_engine.settings import _get_env_overrides

        value = f"probe-{env_var.lower()}"
        monkeypatch.setenv(env_var, value)
        node = _get_env_overrides()
        for key in _OVERRIDE_PATHS[env_var]:
            node = node[key]
        assert node == value

    def test_seed_accounts_reach_the_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dfe_engine.settings import _get_env_overrides

        accounts = [{"username": "probe", "password": "probe-password", "groups": []}]
        monkeypatch.setenv(_SEED_ACCOUNTS, json.dumps(accounts))
        assert _get_env_overrides()["auth"]["local"]["seed_accounts"] == accounts

    def test_hunt_runner_password_reaches_the_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dfe_engine.settings import provided_service_password

        monkeypatch.setenv(_HUNT_RUNNER_PASSWORD, "probe-hunt-runner")
        assert provided_service_password("hunt_runner") == "probe-hunt-runner"

    def test_contract_no_keda(self) -> None:
        # dfe-engine is the control plane -- HPA on CPU is sufficient, no KEDA.
        assert engine_deployment_contract().keda is None

    def test_contract_round_trips_through_json(self) -> None:
        """Contract must serialise to JSON and back without losing fidelity."""
        from scalo.deployment import DeploymentContract

        original = engine_deployment_contract()
        as_json = original.to_json()
        restored = DeploymentContract.model_validate_json(as_json)
        assert restored.model_dump() == original.model_dump()


# ---------------------------------------------------------------------------
# Generator outputs
# ---------------------------------------------------------------------------


class TestArtefactGeneration:
    """scalo generators emit non-empty, well-formed artefacts."""

    def test_generate_artefacts_writes_the_contract(self, tmp_path: Path) -> None:
        # The command hyperi-ci's Build job runs to emit the contract the thin chart is assembled from.
        from dfe_engine.api import _DfeEngineApp

        with pytest.raises(SystemExit) as exited:
            _DfeEngineApp()._make_app().cli(["generate-artefacts", "--output-dir", str(tmp_path)])
        assert exited.value.code == 0
        written = (tmp_path / "deployment-contract.json").read_text(encoding="utf-8")
        assert written == engine_deployment_contract().to_json()
        assert json.loads(written)["schema_version"] == 4

    def test_runtime_stage_includes_contract_points(self) -> None:
        from scalo.deployment import generate_runtime_stage

        contract = engine_deployment_contract()
        stage = generate_runtime_stage(contract)

        assert f"FROM {contract.base_image}" in stage
        assert "EXPOSE 9090" in stage  # observability port (#106)
        assert "8000" in stage  # http traffic port, exposed as an extra port
        assert "/livez" in stage  # healthcheck path (#106)
        assert 'org.opencontainers.image.title="dfe-engine"' in stage

    def test_container_manifest_is_valid_json(self) -> None:
        from scalo.deployment import generate_container_manifest

        contract = engine_deployment_contract()
        manifest = json.loads(generate_container_manifest(contract))
        assert manifest["app_name"] == "dfe-engine"
        assert manifest["binary_name"] == "dfe-engine"
        # metrics/obs port first, then the http traffic extra port (#106).
        assert manifest["expose_ports"] == [9090, 8000]
        assert manifest["healthcheck"]["path"] == contract.health.liveness_path
        assert manifest["entrypoint"] == [contract.binary()]
        assert manifest["cmd"] == contract.entrypoint_args

    def test_argocd_application_is_valid_yaml(self) -> None:
        from scalo.deployment import ArgocdConfig, generate_argocd_application

        contract = engine_deployment_contract()
        argo = ArgocdConfig(
            repo_url="https://github.com/hyperi-io/dfe-engine", dest_namespace="dfe"
        )
        rendered = generate_argocd_application(contract, argo)

        # Strip the autogenerated comment header before YAML parsing.
        doc = yaml.safe_load(rendered)
        assert doc["apiVersion"] == "argoproj.io/v1alpha1"
        assert doc["kind"] == "Application"
        assert doc["metadata"]["name"] == "dfe-engine"
        assert doc["spec"]["source"]["repoURL"].endswith("/dfe-engine")
        assert doc["spec"]["destination"]["namespace"] == "dfe"
        # Sync policy must be automated with prune + selfHeal for GitOps.
        sync = doc["spec"]["syncPolicy"]
        assert sync["automated"]["prune"] is True
        assert sync["automated"]["selfHeal"] is True

    def test_chart_generation_creates_expected_files(self, tmp_path: Path) -> None:
        from scalo.deployment import generate_chart

        contract = engine_deployment_contract()
        generate_chart(contract, tmp_path)

        assert (tmp_path / "Chart.yaml").exists()
        assert (tmp_path / "values.yaml").exists()
        for template in (
            "_helpers.tpl",
            "deployment.yaml",
            "service.yaml",
            "serviceaccount.yaml",
            "secret.yaml",
        ):
            assert (tmp_path / "templates" / template).exists(), f"missing {template}"

    def test_chart_values_reference_correct_image(self, tmp_path: Path) -> None:
        from scalo.deployment import generate_chart

        contract = engine_deployment_contract()
        generate_chart(contract, tmp_path)

        values = (tmp_path / "values.yaml").read_text()
        assert f"{contract.image_registry}/{contract.app_name}" in values


# ---------------------------------------------------------------------------
# Drift detection -- committed artefacts must match the contract
# ---------------------------------------------------------------------------


class TestCommittedArtefactDrift:
    """Committed Dockerfile / chart values must stay aligned with the contract.

    These guard against the contract and the deployed artefacts silently
    drifting apart. If you change the contract, regenerate artefacts and
    commit them; if you tweak Dockerfile/chart by hand, update the contract.
    """

    def test_committed_dockerfile_matches_contract(self) -> None:
        from scalo.deployment import validate_dockerfile

        mismatches = validate_dockerfile(engine_deployment_contract(), PROJECT_ROOT / "Dockerfile")
        assert mismatches == [], f"committed Dockerfile drifted from contract: {mismatches}"

    def test_committed_chart_matches_contract(self) -> None:
        from scalo.deployment import validate_helm_values

        mismatches = validate_helm_values(engine_deployment_contract(), PROJECT_ROOT / "chart")
        assert mismatches == [], f"committed chart values drifted from contract: {mismatches}"

    def test_committed_chart_yaml_name_matches_contract(self) -> None:
        chart = yaml.safe_load((PROJECT_ROOT / "chart" / "Chart.yaml").read_text())
        assert chart["name"] == engine_deployment_contract().app_name


# ---------------------------------------------------------------------------
# Round-trip check -- contract output is deterministic
# ---------------------------------------------------------------------------


def test_artefact_generation_is_deterministic(tmp_path: Path) -> None:
    """Generating twice produces byte-identical output."""
    from scalo.deployment import (
        ArgocdConfig,
        generate_argocd_application,
        generate_container_manifest,
        generate_runtime_stage,
    )

    contract = engine_deployment_contract()
    argo = ArgocdConfig(repo_url="https://github.com/hyperi-io/dfe-engine")

    pairs = [
        (generate_runtime_stage(contract), generate_runtime_stage(contract)),
        (generate_container_manifest(contract), generate_container_manifest(contract)),
        (
            generate_argocd_application(contract, argo),
            generate_argocd_application(contract, argo),
        ),
    ]
    for first, second in pairs:
        assert first == second
