"""Tests for Argo CD Application and AppProject CRD generation."""

from dfe_engine.helm.argo_app import (
    generate_application,
    generate_applications,
    generate_appproject,
)


class TestGenerateApplication:
    def _make(self, **overrides):
        defaults = {
            "service": "receiver",
            "instance": "production",
            "environment_name": "production",
            "namespace": "dfe",
            "argo_project": "dfe",
            "chart_repo_url": "https://charts.example.com/dfe",
            "chart_name": "dfe-receiver",
            "chart_version": "1.2.0",
            "values_path": "values/receiver-production-values.yaml",
        }
        defaults.update(overrides)
        return generate_application(**defaults)

    def test_api_version_and_kind(self):
        app = self._make()
        assert app["apiVersion"] == "argoproj.io/v1alpha1"
        assert app["kind"] == "Application"

    def test_metadata_name(self):
        app = self._make()
        assert app["metadata"]["name"] == "dfe-receiver-production"

    def test_metadata_namespace(self):
        app = self._make()
        assert app["metadata"]["namespace"] == "argocd"

    def test_labels(self):
        app = self._make()
        labels = app["metadata"]["labels"]
        assert labels["app.kubernetes.io/managed-by"] == "dfe-engine"
        assert labels["dfe.hyperi.io/service"] == "receiver"
        assert labels["dfe.hyperi.io/instance"] == "production"
        assert labels["dfe.hyperi.io/environment"] == "production"

    def test_finalizers(self):
        app = self._make()
        assert "resources-finalizer.argocd.argoproj.io" in app["metadata"]["finalizers"]

    def test_spec_project(self):
        app = self._make(argo_project="custom")
        assert app["spec"]["project"] == "custom"

    def test_source(self):
        app = self._make()
        source = app["spec"]["source"]
        assert source["repoURL"] == "https://charts.example.com/dfe"
        assert source["chart"] == "dfe-receiver"
        assert source["targetRevision"] == "1.2.0"
        assert source["helm"]["valueFiles"] == ["values/receiver-production-values.yaml"]

    def test_destination(self):
        app = self._make(namespace="custom-ns")
        dest = app["spec"]["destination"]
        assert dest["server"] == "https://kubernetes.default.svc"
        assert dest["namespace"] == "custom-ns"

    def test_custom_destination_server(self):
        app = self._make(destination_server="https://k8s.example.com")
        assert app["spec"]["destination"]["server"] == "https://k8s.example.com"

    def test_default_sync_policy(self):
        app = self._make()
        sync = app["spec"]["syncPolicy"]
        assert sync["automated"]["prune"] is True
        assert sync["automated"]["selfHeal"] is True
        assert "CreateNamespace=true" in sync["syncOptions"]
        assert sync["retry"]["limit"] == 5

    def test_custom_sync_policy(self):
        custom = {"automated": {"prune": False}}
        app = self._make(sync_policy=custom)
        assert app["spec"]["syncPolicy"]["automated"]["prune"] is False
        assert "retry" not in app["spec"]["syncPolicy"]

    def test_extra_labels(self):
        app = self._make(extra_labels={"team": "platform"})
        assert app["metadata"]["labels"]["team"] == "platform"
        assert app["metadata"]["labels"]["dfe.hyperi.io/service"] == "receiver"

    def test_extra_annotations(self):
        app = self._make(extra_annotations={"notify": "slack"})
        assert app["metadata"]["annotations"]["notify"] == "slack"

    def test_no_annotations_by_default(self):
        app = self._make()
        assert "annotations" not in app["metadata"]

    def test_ignore_differences(self):
        diffs = [{"group": "apps", "kind": "Deployment", "jsonPointers": ["/spec/replicas"]}]
        app = self._make(ignore_differences=diffs)
        assert app["spec"]["ignoreDifferences"] == diffs

    def test_no_ignore_differences_by_default(self):
        app = self._make()
        assert "ignoreDifferences" not in app["spec"]

    def test_loader_service(self):
        app = self._make(
            service="loader",
            instance="staging",
            chart_name="dfe-loader",
            values_path="values/loader-staging-values.yaml",
        )
        assert app["metadata"]["name"] == "dfe-loader-staging"
        assert app["spec"]["source"]["chart"] == "dfe-loader"


class TestGenerateApplications:
    def _make(self, services, **overrides):
        defaults = {
            "environment_name": "production",
            "namespace": "dfe",
            "argo_project": "dfe",
            "chart_repo_url": "https://charts.example.com/dfe",
            "chart_version": "1.2.0",
        }
        defaults.update(overrides)
        return generate_applications(services=services, **defaults)

    def test_generates_one_per_service(self):
        services = [("receiver", "production"), ("loader", "production")]
        apps = self._make(services)
        assert len(apps) == 2
        names = {a["metadata"]["name"] for a in apps}
        assert names == {"dfe-receiver-production", "dfe-loader-production"}

    def test_convention_chart_names(self):
        services = [("receiver", "production"), ("archiver", "production")]
        apps = self._make(services)
        charts = {a["spec"]["source"]["chart"] for a in apps}
        assert charts == {"dfe-receiver", "dfe-archiver"}

    def test_chart_name_override(self):
        services = [("receiver", "production")]
        apps = self._make(services, chart_overrides={"receiver": "custom-chart"})
        assert apps[0]["spec"]["source"]["chart"] == "custom-chart"

    def test_values_path_prefix(self):
        services = [("receiver", "production")]
        apps = self._make(services, values_path_prefix="helm/values")
        assert apps[0]["spec"]["source"]["helm"]["valueFiles"] == [
            "helm/values/receiver-production-values.yaml"
        ]

    def test_sorted_output(self):
        services = [("loader", "prod"), ("archiver", "prod"), ("receiver", "prod")]
        apps = self._make(services)
        names = [a["metadata"]["name"] for a in apps]
        assert names == sorted(names)

    def test_multi_segment_service_name(self):
        services = [("transform-vector", "production")]
        apps = self._make(services)
        assert apps[0]["metadata"]["name"] == "dfe-transform-vector-production"
        assert apps[0]["spec"]["source"]["chart"] == "dfe-transform-vector"

    def test_empty_services(self):
        apps = self._make([])
        assert apps == []

    def test_extra_labels_applied_to_all(self):
        services = [("receiver", "prod"), ("loader", "prod")]
        apps = self._make(services, extra_labels={"team": "platform"})
        for app in apps:
            assert app["metadata"]["labels"]["team"] == "platform"


class TestGenerateAppproject:
    def test_api_version_and_kind(self):
        project = generate_appproject(
            project_name="dfe",
            environment_name="production",
            namespace="dfe",
            source_repos=["https://charts.example.com/dfe"],
        )
        assert project["apiVersion"] == "argoproj.io/v1alpha1"
        assert project["kind"] == "AppProject"

    def test_metadata(self):
        project = generate_appproject(
            project_name="dfe",
            environment_name="production",
            namespace="dfe",
            source_repos=["https://example.com"],
        )
        assert project["metadata"]["name"] == "dfe"
        assert project["metadata"]["namespace"] == "argocd"

    def test_destinations(self):
        project = generate_appproject(
            project_name="dfe",
            environment_name="production",
            namespace="dfe",
            source_repos=["https://example.com"],
        )
        dests = project["spec"]["destinations"]
        assert len(dests) == 1
        assert dests[0]["server"] == "https://kubernetes.default.svc"
        assert dests[0]["namespace"] == "dfe"

    def test_custom_destination_server(self):
        project = generate_appproject(
            project_name="dfe",
            environment_name="production",
            namespace="dfe",
            source_repos=["https://example.com"],
            destination_server="https://k8s.custom.io",
        )
        assert project["spec"]["destinations"][0]["server"] == "https://k8s.custom.io"

    def test_source_repos(self):
        repos = ["https://charts.example.com/dfe", "https://git.example.com/config"]
        project = generate_appproject(
            project_name="dfe",
            environment_name="production",
            namespace="dfe",
            source_repos=repos,
        )
        assert project["spec"]["sourceRepos"] == repos

    def test_default_description(self):
        project = generate_appproject(
            project_name="dfe",
            environment_name="staging",
            namespace="dfe",
            source_repos=["https://example.com"],
        )
        assert project["spec"]["description"] == "DFE Engine - staging"

    def test_custom_description(self):
        project = generate_appproject(
            project_name="dfe",
            environment_name="production",
            namespace="dfe",
            source_repos=["https://example.com"],
            description="Custom project description",
        )
        assert project["spec"]["description"] == "Custom project description"

    def test_roles_integration(self):
        from dfe_engine.helm.argo_rbac import generate_appproject_roles

        roles = generate_appproject_roles()
        project = generate_appproject(
            project_name="dfe",
            environment_name="production",
            namespace="dfe",
            source_repos=["https://example.com"],
            roles=roles,
        )
        assert "roles" in project["spec"]
        assert len(project["spec"]["roles"]) > 0

    def test_no_roles_when_none(self):
        project = generate_appproject(
            project_name="dfe",
            environment_name="production",
            namespace="dfe",
            source_repos=["https://example.com"],
        )
        assert "roles" not in project["spec"]

    def test_cluster_resource_whitelist(self):
        whitelist = [{"group": "", "kind": "Namespace"}]
        project = generate_appproject(
            project_name="dfe",
            environment_name="production",
            namespace="dfe",
            source_repos=["https://example.com"],
            cluster_resource_whitelist=whitelist,
        )
        assert project["spec"]["clusterResourceWhitelist"] == whitelist

    def test_no_whitelist_by_default(self):
        project = generate_appproject(
            project_name="dfe",
            environment_name="production",
            namespace="dfe",
            source_repos=["https://example.com"],
        )
        assert "clusterResourceWhitelist" not in project["spec"]
