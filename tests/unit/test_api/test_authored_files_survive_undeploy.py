#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_authored_files_survive_undeploy.py
#  Purpose:      An authored transform program outlives its instance going away and back
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Undeploying a transform instance parks the files an operator wrote into it.

The instance overlay is derived state the derive path removes whenever its source
stops being live, and the program in its file sets is the one part of it nobody
can derive. Real local deploy repo, real API.
"""

import fnmatch

from dfe_engine.appmgmt import instances
from tests.unit.test_api.test_app_contracts import _deploy_vrl, _wire

SOURCE = "edge"
FILES = f"/api/v1/apps/dfe-transform-vrl/{SOURCE}/files/transforms"
PROGRAM = "010_probe.vrl"
CONTENT = ".a = 1\n"
APP = instances.AppInstance(service="dfe-transform-vrl", instance=SOURCE)

# The deploy repo glob dfe-infra's layer2-apps ApplicationSet turns into Applications.
APPLICATIONSET_GLOB = "values/*-values.yaml"


def _set_state(client, headers, state: str):
    resp = client.patch(f"/api/v1/sources/{SOURCE}", json={"state": state}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _programs(client, headers) -> list[tuple[str, str]]:
    listed = client.get(FILES, headers=headers)
    assert listed.status_code == 200, listed.text
    names = [entry["name"] for entry in listed.json()]
    return [(n, client.get(f"{FILES}/{n}", headers=headers).json()["content"]) for n in names]


def _author(client, headers) -> None:
    resp = client.put(f"{FILES}/{PROGRAM}", json={"content": CONTENT}, headers=headers)
    assert resp.status_code == 200, resp.text


def test_a_program_comes_back_with_its_instance(client, app, admin_headers, tmp_path):
    gc = _wire(app, tmp_path)
    assert _deploy_vrl(client, admin_headers, SOURCE).status_code == 200
    _author(client, admin_headers)

    dormant = _set_state(client, admin_headers, "dormant")
    assert f"dfe-transform-vrl/{SOURCE}: undeploy instance" in dormant["apps_synced"]
    assert APP.overlay_name not in gc.list("helmvars")
    assert APP.parked_name in gc.list("helmvars")

    active = _set_state(client, admin_headers, "active")

    assert f"dfe-transform-vrl/{SOURCE}: deploy instance" in active["apps_synced"]
    assert _programs(client, admin_headers) == [(PROGRAM, CONTENT)]
    assert APP.parked_name not in gc.list("helmvars")


def test_the_parked_copy_deploys_nothing(client, app, admin_headers, tmp_path):
    gc = _wire(app, tmp_path)
    _deploy_vrl(client, admin_headers, SOURCE)
    _author(client, admin_headers)

    _set_state(client, admin_headers, "dormant")

    parked_file = f"values/{APP.parked_name}.yaml"
    assert (gc.repo_path / parked_file).is_file()
    assert not fnmatch.fnmatch(parked_file, APPLICATIONSET_GLOB)
    assert "deploy" not in gc.get("helmvars", APP.parked_name)
    assert APP not in instances.list_instances(gc)
    listed = client.get("/api/v1/apps", headers=admin_headers).json()
    vrl = next(entry for entry in listed if entry["service"] == "dfe-transform-vrl")
    assert vrl["instances"] == []


def test_deleting_the_source_takes_the_parked_copy_with_it(client, app, admin_headers, tmp_path):
    gc = _wire(app, tmp_path)
    _deploy_vrl(client, admin_headers, SOURCE)
    _author(client, admin_headers)
    _set_state(client, admin_headers, "dormant")
    assert APP.parked_name in gc.list("helmvars")

    resp = client.delete(f"/api/v1/sources/{SOURCE}", headers=admin_headers)

    assert resp.status_code == 200, resp.text
    assert f"dfe-transform-vrl/{SOURCE}: drop parked files" in resp.json()["apps_synced"]
    assert APP.parked_name not in gc.list("helmvars")


def test_deleting_a_live_source_parks_nothing(client, app, admin_headers, tmp_path):
    gc = _wire(app, tmp_path)
    _deploy_vrl(client, admin_headers, SOURCE)
    _author(client, admin_headers)

    client.delete(f"/api/v1/sources/{SOURCE}", headers=admin_headers)

    assert APP.overlay_name not in gc.list("helmvars")
    assert APP.parked_name not in gc.list("helmvars")


def test_an_instance_with_no_program_parks_nothing(client, app, admin_headers, tmp_path):
    gc = _wire(app, tmp_path)
    _deploy_vrl(client, admin_headers, SOURCE)

    _set_state(client, admin_headers, "dormant")

    assert APP.parked_name not in gc.list("helmvars")


def test_a_program_deleted_while_live_stays_deleted(client, app, admin_headers, tmp_path):
    gc = _wire(app, tmp_path)
    _deploy_vrl(client, admin_headers, SOURCE)
    _author(client, admin_headers)
    _set_state(client, admin_headers, "dormant")
    _set_state(client, admin_headers, "active")
    removed = client.delete(f"{FILES}/{PROGRAM}", headers=admin_headers)
    assert removed.status_code == 200, removed.text

    _set_state(client, admin_headers, "dormant")
    _set_state(client, admin_headers, "active")

    assert _programs(client, admin_headers) == []
    assert APP.parked_name not in gc.list("helmvars")
