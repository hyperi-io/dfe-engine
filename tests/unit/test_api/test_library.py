#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_library.py
#  Purpose:      Tests for the artefact-library router and the linking routes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""End-to-end router behaviour against a real local deploy repo."""

from __future__ import annotations

import base64
import hashlib

import pytest

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore

VRL = "dfe-transform-vrl"
LIB = "/api/v1/library"

V1 = ". = parse_json!(.message)\n"
V2 = ". = parse_json!(.message)\n.ts = now()\n"


def _wire(app, tmp_path):
    """Attach a real local-repo GitCrud in a dev posture."""
    app.state.settings.env = "dev"
    app.state.settings.deployment.target = "kubernetes"
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    return gc


def _create(client, headers, name="parse-syslog", **body):
    payload = {"name": name, "kind": "vrl"}
    payload.update(body)
    return client.post(LIB, json=payload, headers=headers)


def _publish(client, headers, name="parse-syslog", content=V1, **body):
    return client.post(f"{LIB}/{name}/versions", json={"content": content, **body}, headers=headers)


def _deploy(client, headers, instance="syslog"):
    # A vrl instance IS a source's processing step, so the source comes first.
    client.post(
        "/api/v1/sources",
        json={
            "source": instance,
            "match": {"field": "tags.collector.type", "value": instance},
        },
        headers=headers,
    )
    return client.post(
        f"/api/v1/apps/{VRL}/instances", json={"instance": instance}, headers=headers
    )


class TestKinds:
    def test_503_when_gitops_not_configured(self, client, admin_headers):
        resp = client.get(LIB, headers=admin_headers)
        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"

    def test_kinds_are_served_from_the_manifest(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        resp = client.get(f"{LIB}/kinds", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        # Membership is not pinned: a kind arrives as a manifest edit, so an
        # exact-set assertion would break on every one of them.
        by_name = {k["name"]: k for k in resp.json()}
        assert {"vrl", "vector-yaml", "wasm"} <= set(by_name)
        assert by_name["wasm"]["encoding"] == "base64"
        assert by_name["vector-yaml"]["suffixes"] == [".yaml", ".yml"]

    def test_an_undeclared_kind_is_400(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        resp = _create(client, admin_headers, kind="cobol")
        assert resp.status_code == 400
        assert resp.json()["code"] == "unknown_kind"


class TestLifecycle:
    def test_create_empty_then_publish(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        created = _create(client, admin_headers, group="network", description="syslog")
        assert created.status_code == 200, created.text
        assert created.json()["version"] is None

        got = client.get(f"{LIB}/parse-syslog", headers=admin_headers).json()
        assert got["kind"] == "vrl"
        assert got["state"] == "enabled"
        assert got["group"] == "network"
        assert got["versions"] == []

        published = _publish(client, admin_headers)
        assert published.status_code == 200, published.text
        assert published.json()["version"] == 1
        assert client.get(f"{LIB}/parse-syslog", headers=admin_headers).json()["current"] == 1

    def test_create_with_content_publishes_version_one(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        created = _create(client, admin_headers, content=V1)
        assert created.status_code == 200, created.text
        assert created.json()["version"] == 1

    def test_creating_twice_conflicts(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers)
        resp = _create(client, admin_headers)
        assert resp.status_code == 409
        assert resp.json()["code"] == "already_exists"

    def test_an_unsafe_name_is_400(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        resp = _create(client, admin_headers, name="../escape")
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_name"

    def test_a_missing_artefact_is_404(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        resp = client.get(f"{LIB}/absent", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "no_such_artifact"

    def test_delete_removes_an_unlinked_artefact(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        assert client.delete(f"{LIB}/parse-syslog", headers=admin_headers).status_code == 200
        assert client.get(f"{LIB}/parse-syslog", headers=admin_headers).status_code == 404

    def test_state_moves_without_publishing(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        resp = client.put(
            f"{LIB}/parse-syslog/state", json={"state": "deprecated"}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        got = client.get(f"{LIB}/parse-syslog", headers=admin_headers).json()
        assert got["state"] == "deprecated"
        assert got["versions"] == [1]

    def test_an_unknown_state_is_400(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers)
        resp = client.put(
            f"{LIB}/parse-syslog/state", json={"state": "retired"}, headers=admin_headers
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_state"


class TestVersions:
    def test_publish_read_and_list(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers)
        _publish(client, admin_headers, content=V1, description="first")
        _publish(client, admin_headers, content=V2, description="second")

        listed = client.get(f"{LIB}/parse-syslog/versions", headers=admin_headers).json()
        assert [v["version"] for v in listed] == [1, 2]
        assert listed[0]["description"] == "first"
        assert listed[0]["published_by"] == "admin"
        assert listed[0]["published_at"] > 0

        one = client.get(f"{LIB}/parse-syslog/versions/1", headers=admin_headers)
        assert one.json()["content"] == V1
        assert one.json()["encoding"] == "text"

    def test_a_version_read_carries_its_digest_as_a_strong_etag(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        resp = client.get(f"{LIB}/parse-syslog/versions/1", headers=admin_headers)
        digest = f"sha256:{hashlib.sha256(V1.encode()).hexdigest()}"
        assert resp.json()["digest"] == digest
        assert resp.headers["ETag"] == f'"{digest}"'

    def test_republishing_identical_content_is_not_a_change(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        resp = _publish(client, admin_headers, content=V1)
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is False
        assert resp.json()["version"] == 1
        assert client.get(f"{LIB}/parse-syslog", headers=admin_headers).json()["versions"] == [1]

    def test_different_content_over_an_existing_version_is_409(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        resp = _publish(client, admin_headers, content=V2, version=1)
        assert resp.status_code == 409
        assert resp.json()["code"] == "version_conflict"

    def test_an_out_of_sequence_version_is_409(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        resp = _publish(client, admin_headers, content=V2, version=9)
        assert resp.status_code == 409
        assert resp.json()["code"] == "version_conflict"

    def test_a_missing_version_is_404(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        resp = client.get(f"{LIB}/parse-syslog/versions/9", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "no_such_version"

    def test_control_characters_are_400(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers)
        resp = _publish(client, admin_headers, content=".a = 1\x00\n")
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_content"

    def test_invalid_base64_is_400(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers, name="mod", kind="wasm")
        resp = _publish(client, admin_headers, name="mod", content="not base64!!")
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_content"

    def test_a_wasm_version_round_trips_base64(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        payload = base64.b64encode(b"\x00asm\x01\x00\x00\x00").decode("ascii")
        _create(client, admin_headers, name="mod", kind="wasm", content=payload)
        got = client.get(f"{LIB}/mod/versions/1", headers=admin_headers).json()
        assert got["content"] == payload
        assert got["encoding"] == "base64"

    def test_rollback_repoints_current_and_keeps_the_newer_version(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        _publish(client, admin_headers, content=V2)
        resp = client.post(
            f"{LIB}/parse-syslog/rollback", json={"version": 1}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        got = client.get(f"{LIB}/parse-syslog", headers=admin_headers).json()
        assert got["current"] == 1
        assert got["versions"] == [1, 2]


class TestMetadata:
    def test_a_metadata_edit_does_not_create_a_version(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        resp = client.patch(
            f"{LIB}/parse-syslog",
            json={"description": "syslog envelope", "labels": {"team": "sec"}, "group": "network"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True

        got = client.get(f"{LIB}/parse-syslog", headers=admin_headers).json()
        assert got["versions"] == [1]
        assert got["current"] == 1
        assert got["description"] == "syslog envelope"
        assert got["labels"] == {"team": "sec"}
        assert got["group"] == "network"

    def test_an_unchanged_edit_reports_no_change(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers, description="syslog")
        resp = client.patch(
            f"{LIB}/parse-syslog", json={"description": "syslog"}, headers=admin_headers
        )
        assert resp.json()["changed"] is False

    def test_an_unsafe_label_is_400(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers)
        resp = client.patch(
            f"{LIB}/parse-syslog", json={"labels": {"team": "a b"}}, headers=admin_headers
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_metadata"


class TestFilters:
    def _seed(self, client, headers):
        _create(client, headers, name="parse-syslog", group="network", description="syslog lines")
        client.patch(
            f"{LIB}/parse-syslog", json={"labels": {"team": "sec", "tier": "1"}}, headers=headers
        )
        _create(client, headers, name="parse-netflow", group="network", description="netflow v9")
        client.patch(f"{LIB}/parse-netflow", json={"labels": {"team": "net"}}, headers=headers)
        _create(client, headers, name="enrich-hosts", kind="vector-yaml", group="beats")

    def _names(self, client, headers, **params):
        resp = client.get(LIB, params=params, headers=headers)
        assert resp.status_code == 200, resp.text
        return sorted(a["name"] for a in resp.json())

    def test_no_filter_lists_everything(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        self._seed(client, admin_headers)
        assert self._names(client, admin_headers) == [
            "enrich-hosts",
            "parse-netflow",
            "parse-syslog",
        ]

    def test_filter_by_kind(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        self._seed(client, admin_headers)
        assert self._names(client, admin_headers, kind="vector-yaml") == ["enrich-hosts"]

    def test_filter_by_group(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        self._seed(client, admin_headers)
        assert self._names(client, admin_headers, group="beats") == ["enrich-hosts"]

    def test_filter_by_label_key_and_value(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        self._seed(client, admin_headers)
        assert self._names(client, admin_headers, label="team") == [
            "parse-netflow",
            "parse-syslog",
        ]
        assert self._names(client, admin_headers, label="team=sec") == ["parse-syslog"]

    def test_repeated_labels_and_together(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        self._seed(client, admin_headers)
        resp = client.get(
            LIB, params=[("label", "team=sec"), ("label", "tier=1")], headers=admin_headers
        )
        assert [a["name"] for a in resp.json()] == ["parse-syslog"]

    def test_filter_by_state(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        self._seed(client, admin_headers)
        client.put(f"{LIB}/parse-netflow/state", json={"state": "disabled"}, headers=admin_headers)
        assert self._names(client, admin_headers, state="disabled") == ["parse-netflow"]

    def test_text_search_covers_name_and_description(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        self._seed(client, admin_headers)
        assert self._names(client, admin_headers, q="netflow") == ["parse-netflow"]
        assert self._names(client, admin_headers, q="enrich") == ["enrich-hosts"]

    def test_different_filters_and_together(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        self._seed(client, admin_headers)
        assert self._names(client, admin_headers, kind="vrl", group="beats") == []


class TestTags:
    def test_a_tag_points_at_a_version_and_repoints(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        _publish(client, admin_headers, content=V2)

        resp = client.put(
            f"{LIB}/parse-syslog/tags/stable", json={"version": 1}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        assert client.get(f"{LIB}/parse-syslog", headers=admin_headers).json()["tags"] == {
            "stable": 1
        }

        client.put(f"{LIB}/parse-syslog/tags/stable", json={"version": 2}, headers=admin_headers)
        assert client.get(f"{LIB}/parse-syslog", headers=admin_headers).json()["tags"] == {
            "stable": 2
        }

    def test_publishing_never_moves_a_tag(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        client.put(f"{LIB}/parse-syslog/tags/stable", json={"version": 1}, headers=admin_headers)
        _publish(client, admin_headers, content=V2)
        got = client.get(f"{LIB}/parse-syslog", headers=admin_headers).json()
        assert got["tags"] == {"stable": 1}
        assert got["current"] == 2

    def test_a_tag_on_a_missing_version_is_404(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        resp = client.put(
            f"{LIB}/parse-syslog/tags/stable", json={"version": 4}, headers=admin_headers
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "no_such_version"

    def test_deleting_a_tag(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        client.put(f"{LIB}/parse-syslog/tags/stable", json={"version": 1}, headers=admin_headers)
        assert (
            client.delete(f"{LIB}/parse-syslog/tags/stable", headers=admin_headers).status_code
            == 200
        )
        resp = client.delete(f"{LIB}/parse-syslog/tags/stable", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "no_such_tag"


class TestLinking:
    BASE = f"/api/v1/apps/{VRL}/syslog/files/transforms"

    def _link(self, client, headers, **body):
        payload = {"name": "000_parse.vrl", "artifact": "parse-syslog"}
        payload.update(body)
        return client.post(f"{self.BASE}/link", json=payload, headers=headers)

    def _setup(self, client, app, headers, tmp_path):
        gc = _wire(app, tmp_path)
        _create(client, headers, content=V1)
        _deploy(client, headers)
        return gc

    def test_linking_resolves_the_content_and_records_provenance(
        self, client, app, admin_headers, tmp_path
    ):
        gc = self._setup(client, app, admin_headers, tmp_path)
        resp = self._link(client, admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True
        assert resp.json()["reload"] == "roll"
        assert resp.json()["link"]["version"] == 1

        # The chart renders content, so the content has to be in the values.
        overlay = gc.get("helmvars", f"{VRL}-syslog-values")
        assert overlay["transformFiles"][0]["content"] == V1
        assert overlay["transformFileLinks"][0]["artifact"] == "parse-syslog"

        got = client.get(f"{self.BASE}/000_parse.vrl", headers=admin_headers).json()
        assert got["content"] == V1

    def test_a_tag_link_records_the_resolved_version(self, client, app, admin_headers, tmp_path):
        self._setup(client, app, admin_headers, tmp_path)
        _publish(client, admin_headers, content=V2)
        client.put(f"{LIB}/parse-syslog/tags/stable", json={"version": 1}, headers=admin_headers)

        resp = self._link(client, admin_headers, tag="stable")
        assert resp.status_code == 200, resp.text
        link = resp.json()["link"]
        assert (link["tag"], link["version"]) == ("stable", 1)

        listed = client.get(f"{self.BASE}/links", headers=admin_headers).json()
        assert listed[0]["version"] == 1
        assert listed[0]["available_version"] == 1

    def test_linking_an_absent_artefact_is_404(self, client, app, admin_headers, tmp_path):
        self._setup(client, app, admin_headers, tmp_path)
        resp = self._link(client, admin_headers, artifact="absent")
        assert resp.status_code == 404
        assert resp.json()["code"] == "no_such_artifact"

    def test_linking_an_absent_tag_is_404(self, client, app, admin_headers, tmp_path):
        self._setup(client, app, admin_headers, tmp_path)
        resp = self._link(client, admin_headers, tag="nope")
        assert resp.status_code == 404
        assert resp.json()["code"] == "no_such_tag"

    def test_linking_a_disabled_artefact_is_400(self, client, app, admin_headers, tmp_path):
        self._setup(client, app, admin_headers, tmp_path)
        client.put(f"{LIB}/parse-syslog/state", json={"state": "disabled"}, headers=admin_headers)
        resp = self._link(client, admin_headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == "not_linkable"

    def test_a_kind_the_file_set_cannot_read_is_400(self, client, app, admin_headers, tmp_path):
        self._setup(client, app, admin_headers, tmp_path)
        payload = base64.b64encode(b"\x00asm").decode("ascii")
        _create(client, admin_headers, name="mod", kind="wasm", content=payload)
        resp = self._link(client, admin_headers, name="mod.wasm", artifact="mod")
        assert resp.status_code == 400
        assert resp.json()["code"] == "not_linkable"

    def test_relink_rolls_a_fix_out(self, client, app, admin_headers, tmp_path):
        self._setup(client, app, admin_headers, tmp_path)
        self._link(client, admin_headers)
        _publish(client, admin_headers, content=V2)

        resp = client.post(f"{self.BASE}/relink", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert [link["version"] for link in resp.json()["relinked"]] == [2]
        got = client.get(f"{self.BASE}/000_parse.vrl", headers=admin_headers).json()
        assert got["content"] == V2

    def test_relink_with_nothing_to_do_is_not_a_change(self, client, app, admin_headers, tmp_path):
        self._setup(client, app, admin_headers, tmp_path)
        self._link(client, admin_headers)
        resp = client.post(f"{self.BASE}/relink", headers=admin_headers)
        assert resp.json()["changed"] is False
        assert resp.json()["relinked"] == []

    def test_a_hand_edit_over_a_linked_file_reports_drift(
        self, client, app, admin_headers, tmp_path
    ):
        self._setup(client, app, admin_headers, tmp_path)
        self._link(client, admin_headers)
        client.put(
            f"{self.BASE}/000_parse.vrl", json={"content": ".hand = 1\n"}, headers=admin_headers
        )
        (checked,) = client.get(f"{self.BASE}/links", headers=admin_headers).json()
        assert checked["drift"] is True
        assert checked["outdated"] is False

    def test_a_newer_version_reports_outdated_not_drift(self, client, app, admin_headers, tmp_path):
        self._setup(client, app, admin_headers, tmp_path)
        self._link(client, admin_headers)
        _publish(client, admin_headers, content=V2)
        (checked,) = client.get(f"{self.BASE}/links", headers=admin_headers).json()
        assert (checked["drift"], checked["outdated"]) == (False, True)
        assert checked["available_version"] == 2

    def test_deleting_a_linked_file_drops_its_link(self, client, app, admin_headers, tmp_path):
        # A link left behind would put the deleted file back on the next relink.
        self._setup(client, app, admin_headers, tmp_path)
        self._link(client, admin_headers)
        assert client.delete(f"{self.BASE}/000_parse.vrl", headers=admin_headers).status_code == 200
        assert client.get(f"{self.BASE}/links", headers=admin_headers).json() == []

    def test_usage_names_the_instances_that_link_it(self, client, app, admin_headers, tmp_path):
        self._setup(client, app, admin_headers, tmp_path)
        self._link(client, admin_headers)
        found = client.get(f"{LIB}/parse-syslog/usage", headers=admin_headers).json()
        assert len(found) == 1
        assert found[0]["service"] == VRL
        assert found[0]["instance"] == "syslog"
        assert found[0]["file_set"] == "transforms"

    def test_a_linked_artefact_cannot_be_deleted(self, client, app, admin_headers, tmp_path):
        self._setup(client, app, admin_headers, tmp_path)
        self._link(client, admin_headers)
        resp = client.delete(f"{LIB}/parse-syslog", headers=admin_headers)
        assert resp.status_code == 409
        assert resp.json()["code"] == "artifact_in_use"

    def test_an_unlinked_artefact_can_be_deleted_again(self, client, app, admin_headers, tmp_path):
        self._setup(client, app, admin_headers, tmp_path)
        self._link(client, admin_headers)
        client.delete(f"{self.BASE}/000_parse.vrl", headers=admin_headers)
        assert client.delete(f"{LIB}/parse-syslog", headers=admin_headers).status_code == 200


class TestRbac:
    @pytest.mark.parametrize("path", ["", "/kinds", "/parse-syslog"])
    def test_reads_require_auth(self, client, path):
        assert client.get(f"{LIB}{path}").status_code == 401

    def test_viewer_holds_no_library_grant(self, client, app, viewer_headers, tmp_path):
        # infra_viewer deliberately carries no library read, matching every other
        # governed-ops class.
        _wire(app, tmp_path)
        assert client.get(LIB, headers=viewer_headers).status_code == 403

    def test_viewer_cannot_create(self, client, app, viewer_headers, tmp_path):
        _wire(app, tmp_path)
        assert _create(client, viewer_headers).status_code == 403

    def test_linking_needs_the_library_read_grant(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        # Linking resolves content out of the library into the overlay, so the
        # overlay grant alone is not enough.
        from tests.unit.test_api.test_governed_ops import _scoped_headers

        _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        _deploy(client, admin_headers)
        overlay_only = _scoped_headers(
            app,
            api_settings,
            username="helmw",
            role="helm-writer",
            permissions=["helmvars:read", "helmvars:write", "deployment:read"],
        )
        resp = client.post(
            f"/api/v1/apps/{VRL}/syslog/files/transforms/link",
            json={"name": "000_parse.vrl", "artifact": "parse-syslog"},
            headers=overlay_only,
        )
        assert resp.status_code == 403
        assert "library:read" in resp.json()["message"]


class TestGovernedWrite:
    def test_every_mutation_is_one_commit(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        _create(client, admin_headers)
        after_create = gc.head_revision()
        resp = _publish(client, admin_headers, content=V1)
        assert resp.json()["commit_sha"]
        assert gc.head_revision() != after_create

    def test_a_no_op_publish_makes_no_commit(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        head = gc.head_revision()
        _publish(client, admin_headers, content=V1)
        assert gc.head_revision() == head

    def test_a_protected_path_is_refused_without_the_override(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        from tests.unit.test_api.test_governed_ops import _scoped_headers

        _wire(app, tmp_path)
        _create(client, admin_headers)
        client.post(
            "/api/v1/governance/admin/policies",
            json={"name": "lock", "protected": ["library:*:state"]},
            headers=admin_headers,
        )
        writer = _scoped_headers(
            app,
            api_settings,
            username="libw",
            role="lib-writer",
            permissions=["library:read", "library:write"],
        )
        resp = client.put(f"{LIB}/parse-syslog/state", json={"state": "deprecated"}, headers=writer)
        assert resp.status_code == 403
        assert resp.json()["code"] == "protected_var"

    def test_a_stale_if_match_conflicts(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        _create(client, admin_headers, content=V1)
        stale = gc.head_revision()
        _publish(client, admin_headers, content=V2)
        resp = client.post(
            f"{LIB}/parse-syslog/versions",
            json={"content": ". = 3\n"},
            headers={**admin_headers, "If-Match": stale},
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"
