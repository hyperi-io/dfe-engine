#  Project:      dfe-engine
#  File:         tests/unit/test_backing/test_providers.py
#  Purpose:      The provider catalogue and the managed_by derivation it drives
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What a deployment's provider key makes of a backing service.

The derivation is the contract every later step builds on: the 409 guard, the
card variant, the limits checker. So it is pinned over every (mode, provider)
pair the design names, including the empty ones, against the shipped snapshot
rather than a copy of it.
"""

from pathlib import Path

import pytest

from dfe_engine.backing import providers
from dfe_engine.backing.providers import (
    BUNDLED_CATALOGUE,
    CATALOGUE_FILE_ENV,
    ManagedBy,
    ProviderCatalogueError,
    Resolution,
    SetIn,
    load_providers,
    resolve,
)

KAFKA_SIZING = (
    "mode",
    "replicas",
    "storage",
    "storageModel",
    "resources",
    "objectStore",
    "tieredObject",
    "sizing",
    "controllerPool",
)
CLICKHOUSE_SIZING = (
    "mode",
    "replicas",
    "keeper.replicas",
    "storage",
    "storageModel",
    "resources",
    "objectStore",
    "tieredBlock",
)
# An external service leaves mode writable: the declared mode is the way back to DFE.
KAFKA_EXTERNAL = KAFKA_SIZING[1:]
CLICKHOUSE_EXTERNAL = CLICKHOUSE_SIZING[1:]

SHIPPED = load_providers(BUNDLED_CATALOGUE)

MODES = (None, "", "cluster", "single", "disabled", "external")


def _never_read() -> object:
    raise AssertionError("the declared mode was read where the provider key decides it")


def _mode(value: object):
    return lambda: value


class TestKafkaDerivation:
    @pytest.mark.parametrize("mode", MODES)
    def test_no_stated_provider_is_dfe_whatever_the_repo_declares(self, mode):
        """An unset fact is today's deployment, so nothing changes until infra sends one."""
        assert resolve("kafka", "", _mode(mode), catalogue=SHIPPED) == providers.SELF_RUN

    @pytest.mark.parametrize("provider", ["msk", "confluent-cloud", "redpanda-cloud"])
    def test_a_saas_key_is_provider_without_reading_the_mode(self, provider):
        found = resolve("kafka", provider, _never_read, catalogue=SHIPPED)

        assert found.managed_by is ManagedBy.PROVIDER
        assert found.read_only == KAFKA_SIZING

    @pytest.mark.parametrize(
        ("provider", "set_in"),
        [
            ("msk", SetIn.DEPLOYMENT),
            ("confluent-cloud", SetIn.DEPLOYMENT),
            ("redpanda-cloud", SetIn.PROVIDER),
        ],
    )
    def test_each_saas_key_says_where_it_is_sized(self, provider, set_in):
        assert resolve("kafka", provider, _never_read, catalogue=SHIPPED).set_in is set_in

    @pytest.mark.parametrize("provider", ["strimzi", "redpanda"])
    @pytest.mark.parametrize("mode", [None, "", "cluster", "single", "disabled"])
    def test_a_self_run_key_is_dfe_unless_the_repo_declares_external(self, provider, mode):
        assert resolve("kafka", provider, _mode(mode), catalogue=SHIPPED) == providers.SELF_RUN

    @pytest.mark.parametrize("provider", ["strimzi", "redpanda", "kafka-somewhere-else"])
    def test_any_non_saas_key_with_mode_external_is_external(self, provider):
        """A customer's own broker: DFE deploys none, and has no API to it."""
        found = resolve("kafka", provider, _mode("external"), catalogue=SHIPPED)

        assert found == Resolution(ManagedBy.EXTERNAL, SetIn.OPERATOR, KAFKA_EXTERNAL)

    def test_external_by_declared_mode_leaves_that_mode_writable(self):
        """Writing the mode back is how the service returns to DFE, so it is not refused."""
        found = resolve("kafka", "strimzi", _mode("external"), catalogue=SHIPPED)

        assert not found.refuses("mode")
        assert found.refuses("replicas")
        assert found.refuses("")

    @pytest.mark.parametrize("provider", ["msk", "confluent-cloud", "redpanda-cloud"])
    def test_a_provider_managed_service_keeps_its_mode_read_only(self, provider):
        """No deploy-repo mode turns a SaaS broker into one DFE runs."""
        assert resolve("kafka", provider, _never_read, catalogue=SHIPPED).refuses("mode")

    @pytest.mark.parametrize("mode", [None, "", "cluster"])
    def test_an_unlisted_key_without_mode_external_is_dfe(self, mode):
        found = resolve("kafka", "kafka-somewhere-else", _mode(mode), catalogue=SHIPPED)

        assert found == providers.SELF_RUN

    def test_a_mode_written_as_a_map_is_not_external(self):
        assert resolve("kafka", "strimzi", _mode({"x": 1}), catalogue=SHIPPED).managed_by is (
            ManagedBy.DFE
        )


class TestClickHouseDerivation:
    @pytest.mark.parametrize("mode", MODES)
    def test_no_stated_provider_is_dfe(self, mode):
        assert resolve("clickhouse", "", _mode(mode), catalogue=SHIPPED) == providers.SELF_RUN

    def test_clickhouse_cloud_is_provider_sized_in_the_provider(self):
        found = resolve("clickhouse", "clickhouse-cloud", _never_read, catalogue=SHIPPED)

        assert found == Resolution(ManagedBy.PROVIDER, SetIn.PROVIDER, CLICKHOUSE_SIZING)

    def test_the_external_key_is_external_without_reading_the_mode(self):
        found = resolve("clickhouse", "external", _never_read, catalogue=SHIPPED)

        assert found == Resolution(ManagedBy.EXTERNAL, SetIn.OPERATOR, CLICKHOUSE_EXTERNAL)

    @pytest.mark.parametrize("mode", [None, "", "single", "cluster"])
    def test_in_cluster_is_dfe(self, mode):
        assert resolve("clickhouse", "in_cluster", _mode(mode), catalogue=SHIPPED) == (
            providers.SELF_RUN
        )

    def test_in_cluster_with_the_repo_declaring_external_is_external(self):
        """The chart deploys no server at mode external, whatever the dial's key says."""
        found = resolve("clickhouse", "in_cluster", _mode("external"), catalogue=SHIPPED)

        assert found.managed_by is ManagedBy.EXTERNAL


class TestUnknownService:
    def test_a_service_the_catalogue_omits_is_dfe_unless_external(self):
        assert resolve("postgres", "rds", _mode(None), catalogue=SHIPPED) == providers.SELF_RUN

    def test_with_mode_external_it_is_external_and_refuses_nothing(self):
        found = resolve("postgres", "rds", _mode("external"), catalogue=SHIPPED)

        assert found == Resolution(ManagedBy.EXTERNAL, SetIn.OPERATOR, ())
        assert not found.refuses("replicas")


class TestRefuses:
    MANAGED = Resolution(ManagedBy.PROVIDER, SetIn.DEPLOYMENT, ("replicas", "keeper.replicas"))

    @pytest.mark.parametrize(
        "path",
        ["replicas", "keeper.replicas", "keeper", "", "keeper.replicas.extra"],
    )
    def test_the_path_below_it_and_every_parent_are_refused(self, path):
        assert self.MANAGED.refuses(path)

    @pytest.mark.parametrize("path", ["keeper.resources", "replicasExtra", "name", "keep"])
    def test_a_sibling_or_a_shared_string_prefix_is_not(self, path):
        assert not self.MANAGED.refuses(path)

    @pytest.mark.parametrize("path", ["", "replicas", "storage.size"])
    def test_self_run_refuses_nothing(self, path):
        assert not providers.SELF_RUN.refuses(path)


class TestResolutionOrder:
    def test_an_explicit_path_wins(self, tmp_path, monkeypatch):
        named = tmp_path / "named.yaml"
        named.write_text("services:\n  kafka:\n    providers:\n      a: {managed_by: dfe}\n")
        other = tmp_path / "env.yaml"
        other.write_text("services:\n  kafka:\n    providers:\n      b: {managed_by: dfe}\n")
        monkeypatch.setenv(CATALOGUE_FILE_ENV, str(other))

        assert set(load_providers(named)["kafka"].providers) == {"a"}

    def test_the_environment_beats_the_bundled_snapshot(self, tmp_path, monkeypatch):
        mounted = tmp_path / "providers.yaml"
        mounted.write_text(
            "services:\n  kafka:\n    providers:\n"
            "      new-saas: {managed_by: provider, set_in: provider, read_only: [replicas]}\n"
        )
        monkeypatch.setenv(CATALOGUE_FILE_ENV, str(mounted))

        found = load_providers()

        assert set(found) == {"kafka"}
        assert found["kafka"].providers["new-saas"].read_only == ("replicas",)

    def test_without_either_the_bundled_snapshot_is_read(self, monkeypatch):
        monkeypatch.delenv(CATALOGUE_FILE_ENV, raising=False)

        assert load_providers() == SHIPPED

    def test_a_named_file_that_is_missing_is_refused(self, tmp_path, monkeypatch):
        """A deployment that meant to mount one and did not is told so, not defaulted."""
        monkeypatch.setenv(CATALOGUE_FILE_ENV, str(tmp_path / "absent.yaml"))

        with pytest.raises(ProviderCatalogueError, match="not found"):
            load_providers()

    def test_the_bundled_snapshot_ships_in_the_package(self):
        assert BUNDLED_CATALOGUE.is_file()
        assert BUNDLED_CATALOGUE.parent == Path(providers.__file__).parent


class TestMalformedCatalogue:
    @pytest.mark.parametrize(
        ("body", "said"),
        [
            ("- services\n", "not a mapping"),
            ("services: {}\n", "declares no services"),
            ("services:\n  kafka: [1]\n", "must be a mapping"),
            ("services:\n  kafka:\n    providers: [a]\n", "mapping of key to entry"),
            ("services:\n  kafka:\n    providers:\n      a: {managed_by: dfe-ish}\n", "valid:"),
            (
                "services:\n  kafka:\n    providers:\n      a: {managed_by: provider}\n",
                "sized in the deployment or in the provider",
            ),
            (
                "services:\n  kafka:\n    providers:\n"
                "      a: {managed_by: provider, set_in: operator}\n",
                "sized in the deployment or in the provider",
            ),
            (
                "services:\n  kafka:\n    providers:\n"
                "      a: {managed_by: dfe, read_only: [replicas]}\n",
                "may not declare read_only",
            ),
            (
                "services:\n  kafka:\n    providers:\n"
                "      a: {managed_by: external, set_in: provider}\n",
                "may not declare set_in",
            ),
            (
                "services:\n  kafka:\n    providers:\n"
                "      a: {managed_by: provider, set_in: provider, read_only: replicas}\n",
                "must be a list",
            ),
            (
                "services:\n  kafka:\n    providers:\n"
                "      a: {managed_by: provider, set_in: provider, read_only: [keeper..replicas]}\n",
                "dot-path",
            ),
            ("services:\n  kafka:\n    external: {read_only: ['']}\n", "dot-path"),
        ],
    )
    def test_a_malformed_catalogue_is_refused_with_the_reason(self, tmp_path, body, said):
        path = tmp_path / "providers.yaml"
        path.write_text(body)

        with pytest.raises(ProviderCatalogueError, match=said):
            load_providers(path)


class TestShippedSnapshot:
    def test_it_covers_every_service_the_api_reports(self):
        from dfe_engine.api.v1.backing_services import CATALOGUE

        assert {spec.service for spec in CATALOGUE} <= set(SHIPPED)

    def test_every_dial_key_the_design_names_is_listed(self):
        assert set(SHIPPED["kafka"].providers) == {
            "strimzi",
            "redpanda",
            "msk",
            "confluent-cloud",
            "redpanda-cloud",
        }
        assert set(SHIPPED["clickhouse"].providers) == {
            "in_cluster",
            "clickhouse-cloud",
            "external",
        }

    def test_the_loaded_catalogue_is_the_shipped_one_by_default(self):
        assert providers.PROVIDER_CATALOGUE == SHIPPED
