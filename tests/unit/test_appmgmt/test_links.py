#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_links.py
#  Purpose:      Tests for linking an instance's consumed files to the library
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Resolution, re-resolution, drift and usage, over a real git repo."""

from __future__ import annotations

import base64

import pytest

from dfe_engine.appmgmt import catalogue, files, instances, library, links
from dfe_engine.appmgmt.instances import HELMVARS_CLASS
from dfe_engine.appmgmt.links import LIBRARY_CLASS
from dfe_engine.gitcrud.engine import ResourceNotFoundError

VRL = "dfe-transform-vrl"
VECTOR = "dfe-transform-vector"

V1 = ". = parse_json!(.message)\n"
V2 = ". = parse_json!(.message)\n.ts = now()\n"


@pytest.fixture
def vrl_set():
    return catalogue.file_set(VRL, "transforms")


@pytest.fixture
def overlay():
    return instances.initial_overlay(instances.instance_of(VRL, "syslog"))


# Content lives in files beside the manifest, so a fake library has to serve it
# separately. Keyed by digest, which is unique across every artefact here.
_BLOBS: dict[str, str] = {}


def _publish(env: dict, content: str) -> int:
    version, _changed, _plan = library.publish(env, content=content, actor="alice")
    found = library.read_version(env, version)
    artifact_kind = library.artifact_kind_of(env)
    _BLOBS[found.digest] = library.as_text(
        artifact_kind, library.canonical_bytes(artifact_kind, content)
    )
    return version


def _artifact(*contents: str, kind: str = "vrl", state=library.LifecycleState.ENABLED) -> dict:
    env = library.new_artifact(library.kind(kind), state=state)
    for content in contents:
        _publish(env, content)
    return env


def _source(**artifacts: dict) -> links.LibrarySource:
    def _content(name: str, path: str) -> str:
        for found in library.versions(artifacts[name]):
            if found.path == path:
                return _BLOBS[found.digest]
        raise ResourceNotFoundError(path)

    return links.LibrarySource(envelope=lambda name: artifacts.get(name), content=_content)


def _one(env: dict, name: str = "parse") -> links.LibrarySource:
    return _source(**{name: env})


class TestFileSetDeclaresItsLinksPath:
    def test_links_path_comes_from_the_manifest(self, vrl_set):
        assert vrl_set.values_path == "fileSets.transforms.files"
        assert vrl_set.links_path == "transformFileLinks"

    def test_an_undeclared_links_path_defaults_beside_the_values_path(self):
        raw = {"name": "x", "values_path": "someFiles", "suffixes": [".x"], "language": "x"}
        assert catalogue._file_set_from("svc", raw).links_path == "someFilesLinks"


class TestResolve:
    def test_linking_writes_the_content_and_the_provenance(self, overlay, vrl_set):
        env = _artifact(V1)
        assert (
            links.resolve(
                overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env)
            )
            is True
        )
        assert files.read_file(overlay, vrl_set, "000.vrl").content == V1
        link = links.read_link(overlay, vrl_set, "000.vrl")
        assert (link.artifact, link.version, link.tag) == ("parse", 1, "")
        assert link.digest == library.digest_of(library.kind("vrl"), V1)

    def test_linking_defaults_to_the_current_version(self, overlay, vrl_set):
        env = _artifact(V1, V2)
        links.resolve(overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
        assert links.read_link(overlay, vrl_set, "000.vrl").version == 2

    def test_a_pinned_version_is_honoured(self, overlay, vrl_set):
        env = _artifact(V1, V2)
        links.resolve(
            overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env), version=1
        )
        assert files.read_file(overlay, vrl_set, "000.vrl").content == V1

    def test_a_tag_link_records_the_resolved_version(self, overlay, vrl_set):
        env = _artifact(V1, V2)
        library.set_tag(env, "stable", 1)
        links.resolve(
            overlay,
            vrl_set,
            name="000.vrl",
            artifact="parse",
            env=env,
            source=_one(env),
            tag="stable",
        )
        link = links.read_link(overlay, vrl_set, "000.vrl")
        assert (link.tag, link.version) == ("stable", 1)
        assert link.digest == library.digest_of(library.kind("vrl"), V1)
        assert files.read_file(overlay, vrl_set, "000.vrl").content == V1

    def test_relinking_the_same_thing_is_not_a_change(self, overlay, vrl_set):
        env = _artifact(V1)
        links.resolve(overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
        assert (
            links.resolve(
                overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env)
            )
            is False
        )

    def test_an_empty_artefact_cannot_be_linked(self, overlay, vrl_set):
        env = _artifact()
        with pytest.raises(links.ArtifactNotLinkableError, match="no published version"):
            links.resolve(
                overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env)
            )

    def test_a_disabled_artefact_cannot_be_linked(self, overlay, vrl_set):
        env = _artifact(V1, state=library.LifecycleState.DISABLED)
        with pytest.raises(links.ArtifactNotLinkableError, match="disabled"):
            links.resolve(
                overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env)
            )

    def test_a_deprecated_artefact_may_still_be_linked(self, overlay, vrl_set):
        env = _artifact(V1, state=library.LifecycleState.DEPRECATED)
        assert (
            links.resolve(
                overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env)
            )
            is True
        )

    def test_a_kind_the_file_set_cannot_read_is_refused(self, overlay, vrl_set):
        # A compiled module has no business in a directory the app reads as VRL.
        env = _artifact(base64.b64encode(b"\x00asm").decode("ascii"), kind="wasm")
        with pytest.raises(files.InvalidFilenameError, match=r"\.vrl"):
            links.resolve(
                overlay, vrl_set, name="mod.wasm", artifact="mod", env=env, source=_one(env)
            )

    def test_a_filename_outside_the_kind_is_refused(self, vrl_set):
        # The file set would take a .yaml, but a vrl artefact may not carry one.
        vector_set = catalogue.file_set(VECTOR, "transforms")
        doc = instances.initial_overlay(instances.instance_of(VECTOR, "syslog"))
        env = _artifact(V1)
        with pytest.raises(links.ArtifactNotLinkableError, match="vrl artefact"):
            links.resolve(
                doc, vector_set, name="enrich.yaml", artifact="parse", env=env, source=_one(env)
            )

    def test_an_absent_tag_raises(self, overlay, vrl_set):
        env = _artifact(V1)
        with pytest.raises(library.TagNotFoundError):
            links.resolve(
                overlay,
                vrl_set,
                name="000.vrl",
                artifact="parse",
                env=env,
                source=_one(env),
                tag="nope",
            )


class TestRelink:
    def test_a_version_link_advances_to_the_artefacts_current(self, overlay, vrl_set):
        env = _artifact(V1)
        links.resolve(overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
        _publish(env, V2)

        moved = links.relink(overlay, vrl_set, _source(parse=env))
        assert [m.version for m in moved] == [2]
        assert files.read_file(overlay, vrl_set, "000.vrl").content == V2

    def test_a_tag_link_follows_a_repoint(self, overlay, vrl_set):
        env = _artifact(V1, V2)
        library.set_tag(env, "stable", 1)
        links.resolve(
            overlay,
            vrl_set,
            name="000.vrl",
            artifact="parse",
            env=env,
            source=_one(env),
            tag="stable",
        )
        assert files.read_file(overlay, vrl_set, "000.vrl").content == V1

        library.set_tag(env, "stable", 2)
        moved = links.relink(overlay, vrl_set, _source(parse=env))
        assert [m.version for m in moved] == [2]
        assert links.read_link(overlay, vrl_set, "000.vrl").tag == "stable"
        assert files.read_file(overlay, vrl_set, "000.vrl").content == V2

    def test_a_tag_link_ignores_a_publish_that_did_not_move_the_tag(self, overlay, vrl_set):
        env = _artifact(V1)
        library.set_tag(env, "stable", 1)
        links.resolve(
            overlay,
            vrl_set,
            name="000.vrl",
            artifact="parse",
            env=env,
            source=_one(env),
            tag="stable",
        )
        _publish(env, V2)
        assert links.relink(overlay, vrl_set, _source(parse=env)) == []
        assert files.read_file(overlay, vrl_set, "000.vrl").content == V1

    def test_nothing_to_do_reports_nothing_moved(self, overlay, vrl_set):
        env = _artifact(V1)
        links.resolve(overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
        assert links.relink(overlay, vrl_set, _source(parse=env)) == []

    def test_a_missing_artefact_leaves_the_content_alone(self, overlay, vrl_set):
        # Re-resolving must never delete content an instance is running.
        env = _artifact(V1)
        links.resolve(overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
        assert links.relink(overlay, vrl_set, _source()) == []
        assert files.read_file(overlay, vrl_set, "000.vrl").content == V1

    def test_a_disabled_artefact_is_not_advanced(self, overlay, vrl_set):
        env = _artifact(V1)
        links.resolve(overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
        _publish(env, V2)
        library.set_state(env, library.LifecycleState.DISABLED)
        assert links.relink(overlay, vrl_set, _source(parse=env)) == []
        assert files.read_file(overlay, vrl_set, "000.vrl").content == V1

    def test_relink_restores_content_edited_out_from_under_a_link(self, overlay, vrl_set):
        env = _artifact(V1)
        links.resolve(overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
        files.upsert_file(overlay, vrl_set, "000.vrl", ".hand = 1\n")
        moved = links.relink(overlay, vrl_set, _source(parse=env))
        assert [m.name for m in moved] == ["000.vrl"]
        assert files.read_file(overlay, vrl_set, "000.vrl").content == V1


class TestStatus:
    def test_a_clean_link_reports_neither_drift_nor_staleness(self, overlay, vrl_set):
        env = _artifact(V1)
        links.resolve(overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
        (checked,) = links.status(overlay, vrl_set, _source(parse=env))
        assert (checked.drift, checked.outdated, checked.missing) == (False, False, False)
        assert checked.resolved_digest == checked.link.digest

    def test_a_hand_edit_over_a_linked_file_is_drift(self, overlay, vrl_set):
        env = _artifact(V1)
        links.resolve(overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
        files.upsert_file(overlay, vrl_set, "000.vrl", ".hand = 1\n")
        (checked,) = links.status(overlay, vrl_set, _source(parse=env))
        assert checked.drift is True
        assert checked.resolved_digest != checked.link.digest

    def test_a_newer_version_makes_the_link_outdated_not_drifted(self, overlay, vrl_set):
        env = _artifact(V1)
        links.resolve(overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
        _publish(env, V2)
        (checked,) = links.status(overlay, vrl_set, _source(parse=env))
        assert (checked.drift, checked.outdated) == (False, True)
        assert checked.available_version == 2

    def test_a_deleted_artefact_is_reported_missing(self, overlay, vrl_set):
        env = _artifact(V1)
        links.resolve(overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
        (checked,) = links.status(overlay, vrl_set, _source())
        assert checked.missing is True

    def test_a_deleted_file_under_a_link_shows_no_resolved_digest(self, overlay, vrl_set):
        env = _artifact(V1)
        links.resolve(overlay, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
        files.delete_file(overlay, vrl_set, "000.vrl")
        (checked,) = links.status(overlay, vrl_set, _source(parse=env))
        assert (checked.resolved_digest, checked.drift) == ("", True)


class TestUsage:
    def test_usage_reads_straight_off_the_overlays(self, crud, vrl_set):
        env = _artifact(V1)
        crud.put(LIBRARY_CLASS, "parse", env, actor="alice")
        for instance in ("syslog", "netflow"):
            app = instances.instance_of(VRL, instance)
            doc = instances.initial_overlay(app)
            links.resolve(doc, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
            crud.put(HELMVARS_CLASS, app.overlay_name, doc, actor="alice")

        found = links.usage(crud, "parse")
        assert sorted(u.instance for u in found) == ["netflow", "syslog"]
        assert {u.file_set for u in found} == {"transforms"}
        assert {u.version for u in found} == {1}

    def test_an_unlinked_artefact_is_used_nowhere(self, crud):
        assert links.usage(crud, "parse") == []

    def test_an_instance_with_no_links_is_skipped(self, crud):
        app = instances.instance_of(VRL, "syslog")
        crud.put(HELMVARS_CLASS, app.overlay_name, instances.initial_overlay(app), actor="alice")
        assert links.usage(crud, "parse") == []


class TestGitRoundTrip:
    def test_linking_reads_content_out_of_a_real_bundle(self, crud, vrl_set):
        # The whole path with no fake in it: publish into the deploy repo, then
        # resolve a link through the source that reads files off disk.
        env = library.new_artifact(library.kind("vrl"))
        _version, _changed, plan = library.publish(env, content=V1, actor="alice")
        crud.put_bundle(LIBRARY_CLASS, "parse", env, actor="alice", writes=dict(plan.writes))

        source = links.crud_source(crud)
        doc = instances.initial_overlay(instances.instance_of(VRL, "syslog"))
        assert (
            links.resolve(doc, vrl_set, name="000.vrl", artifact="parse", env=env, source=source)
            is True
        )
        assert files.read_file(doc, vrl_set, "000.vrl").content == V1
        (checked,) = links.status(doc, vrl_set, source)
        assert (checked.drift, checked.missing, checked.outdated) == (False, False, False)

    def test_content_and_provenance_survive_a_commit(self, crud, vrl_set):
        env = _artifact(V1)
        crud.put(LIBRARY_CLASS, "parse", env, actor="alice")
        app = instances.instance_of(VRL, "syslog")
        doc = instances.initial_overlay(app)
        links.resolve(
            doc, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env), version=1
        )
        crud.put(HELMVARS_CLASS, app.overlay_name, doc, actor="alice")

        reloaded = crud.get(HELMVARS_CLASS, app.overlay_name)
        assert files.read_file(reloaded, vrl_set, "000.vrl").content == V1
        assert links.read_link(reloaded, vrl_set, "000.vrl").artifact == "parse"
        (checked,) = links.status(reloaded, vrl_set, links.crud_source(crud))
        assert (checked.drift, checked.missing) == (False, False)

    def test_a_version_pinned_link_stays_a_four_field_entry(self, crud, vrl_set):
        # A tag is written only when the link was made through one, so the common
        # case does not carry an empty key into every diff.
        env = _artifact(V1)
        app = instances.instance_of(VRL, "syslog")
        doc = instances.initial_overlay(app)
        links.resolve(doc, vrl_set, name="000.vrl", artifact="parse", env=env, source=_one(env))
        crud.put(HELMVARS_CLASS, app.overlay_name, doc, actor="alice")
        entry = crud.get(HELMVARS_CLASS, app.overlay_name)["transformFileLinks"][0]
        assert set(entry) == {"name", "artifact", "version", "digest"}

    def test_a_malformed_links_value_is_reported(self, vrl_set):
        with pytest.raises(ValueError, match="expected a list"):
            links.list_links({"transformFileLinks": {"a": 1}}, vrl_set)
