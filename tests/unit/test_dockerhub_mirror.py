#  Project:      dfe-engine
#  File:         tests/unit/test_dockerhub_mirror.py
#  Purpose:      The Docker Hub mirror rewrite leaves every non-Hub and digest-pinned reference intact
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A reference goes through the mirror only when one is set and the image lives on Docker Hub."""

import pytest

from tests.support.dockerhub import MIRROR_ENV, dockerhub_image

MIRROR = "harbor.example:8443/dockerhub"
DIGEST = "sha256:" + "ab" * 32


@pytest.mark.parametrize(
    ("mirror", "reference", "expected"),
    [
        pytest.param(None, "busybox:stable", "busybox:stable", id="unset_official"),
        pytest.param(None, "apache/kafka:4.3.1", "apache/kafka:4.3.1", id="unset_namespaced"),
        pytest.param("", "busybox:stable", "busybox:stable", id="empty"),
        pytest.param("  ", "busybox:stable", "busybox:stable", id="blank"),
        pytest.param(MIRROR, "busybox:stable", f"{MIRROR}/library/busybox:stable", id="official"),
        pytest.param(MIRROR, "busybox", f"{MIRROR}/library/busybox", id="official_untagged"),
        pytest.param(
            MIRROR,
            "redpandadata/redpanda:v25.1.1",
            f"{MIRROR}/redpandadata/redpanda:v25.1.1",
            id="namespaced",
        ),
        pytest.param(
            f"{MIRROR}/",
            "apache/kafka:4.3.1",
            f"{MIRROR}/apache/kafka:4.3.1",
            id="trailing_slash_on_the_mirror",
        ),
        pytest.param(
            MIRROR,
            "ghcr.io/ferretdb/ferretdb-eval:2.7.0",
            "ghcr.io/ferretdb/ferretdb-eval:2.7.0",
            id="registry_host",
        ),
        pytest.param(MIRROR, "localhost/probe:1", "localhost/probe:1", id="localhost_registry"),
        pytest.param(
            MIRROR, "registry.local:5000/probe:1", "registry.local:5000/probe:1", id="registry_port"
        ),
        pytest.param(
            MIRROR,
            f"clickhouse/clickhouse-server:26.3.42.3@{DIGEST}",
            f"{MIRROR}/clickhouse/clickhouse-server:26.3.42.3@{DIGEST}",
            id="digest_stays_pinned",
        ),
        pytest.param(
            MIRROR,
            f"busybox:stable@{DIGEST}",
            f"{MIRROR}/library/busybox:stable@{DIGEST}",
            id="official_digest_stays_pinned",
        ),
    ],
)
def test_the_reference_follows_the_mirror(monkeypatch, mirror, reference, expected):
    if mirror is None:
        monkeypatch.delenv(MIRROR_ENV, raising=False)
    else:
        monkeypatch.setenv(MIRROR_ENV, mirror)

    assert dockerhub_image(reference) == expected
