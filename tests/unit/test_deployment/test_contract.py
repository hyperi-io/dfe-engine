"""Unit tests for dfe-engine's deployment contract.

Verifies that :func:`dfe_engine.deployment_contract.engine_deployment_contract`
is well-formed and that pylib's generators emit valid Dockerfile-runtime,
ArgoCD Application, container manifest, and Helm chart artefacts from it.

These tests are the dfe-engine analogue of dfe-loader's
``tests/integration/helm_contract.rs`` + ``tests/integration/deployment.rs``:
they catch drift between the contract and committed artefacts before CI does.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from dfe_engine.deployment_contract import engine_deployment_contract

PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Contract well-formedness
# ---------------------------------------------------------------------------


class TestContractWellFormed:
    """The contract instantiates and exposes the expected shape."""

    def test_contract_instantiates(self) -> None:
        contract = engine_deployment_contract()
        assert contract.app_name == "dfe-engine"
        assert contract.binary_name == "dfe-api"
        assert contract.metrics_port == 8000
        assert contract.env_prefix == "DFE"
        assert contract.config_mount_path == "/etc/dfe/config"

    def test_contract_health_paths(self) -> None:
        contract = engine_deployment_contract()
        # Probes are served by scalo's health router mounted in api/app.py -
        # /health/live + /health/ready. /api/v1/system/health does not exist.
        assert contract.health.liveness_path == "/health/live"
        assert contract.health.readiness_path == "/health/ready"
        assert contract.health.metrics_path == "/metrics"

    def test_contract_secrets(self) -> None:
        contract = engine_deployment_contract()
        groups = {g.group_name for g in contract.secrets}
        assert groups == {"clickhouse", "jwt"}
        # Env names must be the ones the settings loader reads (nothing parses
        # a DFE__SECTION__KEY form) - see _get_env_overrides in settings.py.
        clickhouse = next(g for g in contract.secrets if g.group_name == "clickhouse")
        assert clickhouse.env_vars[0].env_var == "DFE_CLICKHOUSE_PASSWORD"
        jwt = next(g for g in contract.secrets if g.group_name == "jwt")
        assert jwt.env_vars[0].env_var == "DFE_API_JWT_SECRET"

    def test_contract_secret_env_names_are_read_by_settings_loader(self, monkeypatch) -> None:
        """The contract's env names must round-trip through load_settings."""
        from dfe_engine.settings import load_settings

        monkeypatch.setenv("DFE_CLICKHOUSE_PASSWORD", "ch-secret")
        monkeypatch.setenv("DFE_API_JWT_SECRET", "jwt-secret-key-for-tests-hmac-32b")
        settings = load_settings()
        assert settings.clickhouse.password == "ch-secret"
        assert settings.api.jwt_secret == "jwt-secret-key-for-tests-hmac-32b"

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
    """pylib generators emit non-empty, well-formed artefacts."""

    def test_runtime_stage_includes_contract_points(self) -> None:
        from scalo.deployment import generate_runtime_stage

        contract = engine_deployment_contract()
        stage = generate_runtime_stage(contract)

        assert f"FROM {contract.base_image}" in stage
        assert "EXPOSE 8000" in stage
        assert "/health/live" in stage  # healthcheck path (scalo health router)
        assert 'org.opencontainers.image.title="dfe-engine"' in stage

    def test_container_manifest_is_valid_json(self) -> None:
        from scalo.deployment import generate_container_manifest

        contract = engine_deployment_contract()
        manifest = json.loads(generate_container_manifest(contract))
        assert manifest["app_name"] == "dfe-engine"
        assert manifest["binary_name"] == "dfe-api"
        assert manifest["expose_ports"] == [contract.metrics_port]
        assert manifest["healthcheck"]["path"] == contract.health.liveness_path
        assert manifest["entrypoint"] == [contract.binary()]
        assert manifest["cmd"] == contract.entrypoint_args

    def test_argocd_application_is_valid_yaml(self) -> None:
        from scalo.deployment import (
            ArgocdConfig,
            argocd_repo_url_from_cascade,
            generate_argocd_application,
        )

        contract = engine_deployment_contract()
        argo = ArgocdConfig(repo_url=argocd_repo_url_from_cascade(contract.app_name))
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

    def test_committed_dockerfile_healthcheck_hits_live_route(self) -> None:
        """HEALTHCHECK must curl a route that exists (scalo /health/live) -
        /api/v1/system/health was never served and marked containers unhealthy."""
        text = (PROJECT_ROOT / "Dockerfile").read_text()
        assert "/health/live" in text
        assert "/api/v1/system/health" not in text

    def test_committed_chart_probes_hit_scalo_health_routes(self) -> None:
        text = (PROJECT_ROOT / "chart" / "templates" / "deployment.yaml").read_text()
        assert "path: /health/live" in text
        assert "path: /health/ready" in text
        assert "path: /health/startup" in text
        assert "/api/v1/system/health" not in text

    def test_committed_chart_notes_have_no_dead_health_path(self) -> None:
        text = (PROJECT_ROOT / "chart" / "templates" / "NOTES.txt").read_text()
        assert "/api/v1/system/health" not in text
        assert "/health/" in text


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
