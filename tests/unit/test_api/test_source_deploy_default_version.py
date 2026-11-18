#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_source_deploy_default_version.py
#  Purpose:      A deploy or plan with no version targets the source's current version
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A deploy or plan that names no version targets ``current``, not ``deployed_version``.

A promote creates a new version and moves ``current`` to it. If a bare deploy took
``deployed_version`` it would re-apply the old version, answer 200 and add no column.
Both versions here carry no schema, so each route stops at the 404 that names the
version it resolved, and that name is what separates the two.
"""

import pytest
from fastapi.testclient import TestClient

from dfe_engine.yaml_utils import yaml_dump

SOURCE = "evolving"


@pytest.fixture
def evolving_source(api_settings) -> None:
    """A source deployed at 1.0.0 whose current version is 2.0.0."""
    yaml_dump(
        {
            "source": SOURCE,
            "display_name": "Evolving",
            "enabled": True,
            "match": {"field": "tags.collector.type", "value": SOURCE},
            "current": "2.0.0",
            "deployed_version": "1.0.0",
            "versions": {
                "1.0.0": {"date_time": "2026-01-01"},
                "2.0.0": {"date_time": "2026-01-02"},
            },
        },
        f"{api_settings.source.sources_dir}/{SOURCE}.yaml",
    )


@pytest.mark.parametrize(
    ("version", "resolved"),
    [(None, "2.0.0"), ("1.0.0", "1.0.0")],
    ids=["bare-takes-current", "explicit-version-wins"],
)
@pytest.mark.parametrize(
    ("method", "path", "params"),
    [
        ("POST", f"/api/v1/sources/{SOURCE}/deploy", {"dry_run": "true"}),
        ("POST", f"/api/v1/sources/{SOURCE}/plan", {}),
    ],
    ids=["deploy", "plan"],
)
def test_the_route_resolves_the_version_it_is_given_else_current(
    evolving_source,
    client: TestClient,
    admin_headers: dict,
    method: str,
    path: str,
    params: dict,
    version: str | None,
    resolved: str,
):
    query = {**params, **({"version": version} if version else {})}

    resp = client.request(method, path, params=query, headers=admin_headers)

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "no_schema"
    assert f"version '{resolved}'" in resp.json()["detail"]
