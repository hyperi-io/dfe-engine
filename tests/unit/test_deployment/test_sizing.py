"""Tests for deployment sizing module."""

import pytest

from dfe_engine.deployment.models.common import TShirtSize
from dfe_engine.deployment.sizing import (
    DEFAULT_SIZES,
    RESOURCE_SIZES,
    _ensure_compat_dicts,
    apply_sizing,
    get_resources,
    get_service_overrides,
)
from dfe_engine.deployment.validators import _parse_k8s_quantity


class TestResourceSizes:
    """Verify the unified resource table."""

    def test_all_standard_sizes_present(self):
        expected = {"xs", "small", "medium", "large", "xlarge"}
        assert set(RESOURCE_SIZES.keys()) == expected

    def test_2_to_1_ratio(self):
        """Every size must have exactly 2 GiB per 1 CPU at the limit."""
        for size_name, spec in RESOURCE_SIZES.items():
            cpu_limit = _parse_k8s_quantity(spec.limits.cpu)
            mem_limit_bytes = _parse_k8s_quantity(spec.limits.memory)
            mem_limit_gib = mem_limit_bytes / (1024**3)
            ratio = mem_limit_gib / cpu_limit
            assert ratio == pytest.approx(2.0), (
                f"Size {size_name}: ratio is {ratio}, expected 2.0 "
                f"(cpu={spec.limits.cpu}, mem={spec.limits.memory})"
            )

    def test_requests_half_of_limits(self):
        """Requests should be half of limits (burstable QoS)."""
        for size_name, spec in RESOURCE_SIZES.items():
            req_cpu = _parse_k8s_quantity(spec.requests.cpu)
            lim_cpu = _parse_k8s_quantity(spec.limits.cpu)
            assert req_cpu == pytest.approx(lim_cpu / 2), (
                f"Size {size_name}: CPU request {req_cpu} != limit/2 {lim_cpu/2}"
            )

            req_mem = _parse_k8s_quantity(spec.requests.memory)
            lim_mem = _parse_k8s_quantity(spec.limits.memory)
            assert req_mem == pytest.approx(lim_mem / 2), (
                f"Size {size_name}: memory request {req_mem} != limit/2 {lim_mem/2}"
            )

    def test_each_size_doubles_previous(self):
        """Each size should be 2x the previous."""
        sizes = ["xs", "small", "medium", "large", "xlarge"]
        for i in range(1, len(sizes)):
            prev = RESOURCE_SIZES[sizes[i - 1]]
            curr = RESOURCE_SIZES[sizes[i]]
            prev_cpu = _parse_k8s_quantity(prev.limits.cpu)
            curr_cpu = _parse_k8s_quantity(curr.limits.cpu)
            assert curr_cpu == pytest.approx(prev_cpu * 2), (
                f"Size {sizes[i]}: CPU {curr_cpu} != 2x prev {prev_cpu}"
            )


class TestGetResources:
    def test_valid_size(self):
        spec = get_resources("medium")
        assert spec.limits.cpu == "2"
        assert spec.limits.memory == "4Gi"

    def test_enum_input(self):
        spec = get_resources(TShirtSize.small)
        assert spec.requests.cpu == "500m"

    def test_custom_raises(self):
        with pytest.raises(ValueError, match="custom"):
            get_resources("custom")

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown size"):
            get_resources("xxxl")


class TestGetServiceOverrides:
    @pytest.mark.parametrize("service", ["receiver", "loader", "archiver"])
    @pytest.mark.parametrize("size", ["xs", "small", "medium", "large", "xlarge"])
    def test_all_combinations_return_dict(self, service, size):
        """All 15 service × size combinations must return a dict."""
        overrides = get_service_overrides(service, size)
        assert isinstance(overrides, dict)

    def test_custom_returns_empty(self):
        overrides = get_service_overrides("loader", "custom")
        assert overrides == {}

    def test_loader_sizes_scale(self):
        """Loader flush_bytes should increase with size."""
        sizes = ["xs", "small", "medium", "large", "xlarge"]
        prev_bytes = 0
        for size in sizes:
            overrides = get_service_overrides("loader", size)
            flush_bytes = overrides["buffer"]["flush_bytes"]
            assert flush_bytes > prev_bytes, (
                f"Loader {size}: flush_bytes {flush_bytes} not > previous {prev_bytes}"
            )
            prev_bytes = flush_bytes

    def test_archiver_parallelism_scales(self):
        """Archiver writer_parallelism should increase with size."""
        sizes = ["xs", "small", "medium", "large", "xlarge"]
        prev_par = 0
        for size in sizes:
            overrides = get_service_overrides("archiver", size)
            par = overrides["buffer"]["writer_parallelism"]
            assert par > prev_par, (
                f"Archiver {size}: parallelism {par} not > previous {prev_par}"
            )
            prev_par = par

    def test_receiver_overrides_have_buffer(self):
        overrides = get_service_overrides("receiver", "small")
        assert "buffer" in overrides
        assert overrides["buffer"]["memory_limit"] == 0


class TestApplySizing:
    @pytest.mark.parametrize("service", ["receiver", "loader", "archiver"])
    @pytest.mark.parametrize("size", ["xs", "small", "medium", "large", "xlarge"])
    def test_all_combinations(self, service, size):
        """All 15 combinations produce valid output."""
        deploy_overrides, svc_overrides = apply_sizing(service, size)
        assert "resources" in deploy_overrides
        assert isinstance(svc_overrides, dict)

    def test_deploy_overrides_have_resources(self):
        deploy, _ = apply_sizing("loader", "medium")
        assert deploy["resources"]["limits"]["cpu"] == "2"
        assert deploy["resources"]["limits"]["memory"] == "4Gi"

    def test_deploy_overrides_have_keda(self):
        deploy, _ = apply_sizing("receiver", "small")
        assert "keda" in deploy
        assert deploy["keda"]["max_replicas"] == 10

    def test_unknown_service_raises(self):
        with pytest.raises(ValueError, match="Unknown service"):
            apply_sizing("unknown", "small")


class TestDefaultSizes:
    @pytest.fixture(autouse=True)
    def _init_defaults(self):
        _ensure_compat_dicts()

    def test_receiver_default(self):
        assert DEFAULT_SIZES["receiver"] == TShirtSize.small

    def test_loader_default(self):
        assert DEFAULT_SIZES["loader"] == TShirtSize.medium

    def test_archiver_default(self):
        assert DEFAULT_SIZES["archiver"] == TShirtSize.small
