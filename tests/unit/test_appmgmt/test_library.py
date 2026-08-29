#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_library.py
#  Purpose:      Tests for the versioned artefact library
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Immutable content, mutable metadata and tags, over a real git repo."""

from __future__ import annotations

import base64
import hashlib

import pytest

from dfe_engine.appmgmt import catalogue, library
from dfe_engine.appmgmt.links import LIBRARY_CLASS
from dfe_engine.gitcrud.versioned import VersionConflictError, VersionedDoc
from dfe_engine.yaml_utils import yaml_dump_string

VRL_SOURCE = ". = parse_json!(.message)\n"


@pytest.fixture
def vrl_kind():
    return library.kind("vrl")


@pytest.fixture
def wasm_kind():
    return library.kind("wasm")


@pytest.fixture
def artifact(vrl_kind):
    return library.new_artifact(vrl_kind, group="network", description="parse the envelope")


class TestKinds:
    def test_kinds_come_from_the_manifest(self):
        # Membership is not pinned: a kind arrives as a manifest edit, so an
        # exact-set assertion would break on every one of them.
        by_name = {k.name: k for k in library.kinds()}
        assert {"vrl", "vector-yaml", "wasm"} <= set(by_name)
        assert by_name["vrl"].suffixes == (".vrl",)
        assert by_name["vector-yaml"].suffixes == (".yaml", ".yml")
        assert by_name["wasm"].encoding is catalogue.Encoding.BASE64

    def test_every_declared_kind_is_well_formed(self):
        for kind in library.kinds():
            assert kind.name
            assert kind.language, f"{kind.name} declares no language"
            assert kind.suffixes, f"{kind.name} declares no suffixes"
            assert all(s.startswith(".") for s in kind.suffixes)

    def test_an_undeclared_kind_raises(self):
        with pytest.raises(library.UnknownKindError):
            library.kind("nonesuch")

    def test_a_new_kind_needs_no_code(self, tmp_path):
        # The whole point of the manifest: a kind arrives as data.
        manifest = tmp_path / "apps.yaml"
        manifest.write_text(
            "apps:\n  dfe-loader:\n    multiplicity: single\n"
            "kinds:\n  lua:\n    language: lua\n    suffixes: ['.lua']\n    encoding: text\n"
        )
        loaded = catalogue.load_kinds(manifest)
        assert loaded["lua"].language == "lua"


class TestDigest:
    def test_text_digest_is_over_the_newline_terminated_form(self, vrl_kind):
        # The overlay stores text as a block scalar, which always ends in a
        # newline, so digesting the unterminated form would report false drift.
        expected = hashlib.sha256(b".a = 1\n").hexdigest()
        assert library.digest_of(vrl_kind, ".a = 1") == f"sha256:{expected}"
        assert library.digest_of(vrl_kind, ".a = 1\n") == f"sha256:{expected}"

    def test_base64_digest_is_over_the_decoded_bytes(self, wasm_kind):
        payload = b"\x00asm\x01\x00\x00\x00"
        encoded = base64.b64encode(payload).decode("ascii")
        expected = hashlib.sha256(payload).hexdigest()
        assert library.digest_of(wasm_kind, encoded) == f"sha256:{expected}"

    def test_invalid_base64_is_refused(self, wasm_kind):
        with pytest.raises(library.InvalidArtifactError, match="base64"):
            library.digest_of(wasm_kind, "not base64!!")

    def test_oversized_content_is_refused(self, vrl_kind):
        with pytest.raises(library.InvalidArtifactError, match="limit"):
            library.validate_content(vrl_kind, "x" * (library.MAX_ARTIFACT_BYTES + 1))

    def test_control_characters_are_refused(self, vrl_kind):
        with pytest.raises(library.InvalidArtifactError, match="control characters"):
            library.validate_content(vrl_kind, ".a = 1\x00\n")


class TestPublish:
    def test_first_publish_is_version_one(self, artifact):
        version, changed, _plan = library.publish(artifact, content=VRL_SOURCE, actor="alice")
        assert (version, changed) == (1, True)
        assert library.summarise("parse", artifact).current == 1

    def test_a_version_records_its_file_digest_encoding_and_kind(self, artifact, vrl_kind):
        library.publish(artifact, content=VRL_SOURCE, actor="alice", description="v1")
        found = library.read_version(artifact, 1)
        assert found.path == "versions/0001.vrl"
        assert found.digest == library.digest_of(vrl_kind, VRL_SOURCE)
        assert found.size_bytes == len(VRL_SOURCE.encode("utf-8"))
        assert found.encoding is catalogue.Encoding.TEXT
        assert found.kind == "vrl"
        assert found.description == "v1"
        assert found.published_by == "alice"
        assert found.published_at > 0

    def test_a_publish_plans_the_version_file_and_the_current_copy(self, artifact):
        _v, _changed, plan = library.publish(artifact, content=VRL_SOURCE, actor="alice")
        assert plan.writes == {"versions/0001.vrl": VRL_SOURCE, "current.vrl": VRL_SOURCE}

    def test_the_manifest_carries_no_content(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        assert VRL_SOURCE not in yaml_dump_string(artifact)

    def test_republishing_identical_content_is_a_no_op(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        version, changed, plan = library.publish(artifact, content=VRL_SOURCE, actor="bob")
        assert (version, changed) == (1, False)
        assert plan.writes == {}
        assert library.version_numbers(artifact) == [1]

    def test_different_content_appends_a_version(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        version, changed, _plan = library.publish(artifact, content=".b = 2\n", actor="alice")
        assert (version, changed) == (2, True)
        assert library.version_numbers(artifact) == [1, 2]

    def test_republishing_identical_content_at_its_own_version_is_a_no_op(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        version, changed, _plan = library.publish(
            artifact, content=VRL_SOURCE, actor="alice", version=1
        )
        assert (version, changed) == (1, False)

    def test_different_content_over_an_existing_version_conflicts(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        with pytest.raises(VersionConflictError, match="already holds different content"):
            library.publish(artifact, content=".b = 2\n", actor="alice", version=1)

    def test_an_out_of_sequence_version_conflicts(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        with pytest.raises(VersionConflictError, match="out of sequence"):
            library.publish(artifact, content=".b = 2\n", actor="alice", version=7)

    def test_publishing_at_the_next_number_explicitly_is_accepted(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        version, changed, _plan = library.publish(
            artifact, content=".b = 2\n", actor="alice", version=2
        )
        assert (version, changed) == (2, True)

    def test_a_binary_kind_is_stored_decoded_under_its_own_extension(self, wasm_kind):
        env = library.new_artifact(wasm_kind)
        payload = base64.b64encode(b"\x00asm").decode("ascii")
        _v, _changed, plan = library.publish(env, content=f"  {payload}  ", actor="alice")
        path = library.read_version(env, 1).path
        assert path == "versions/0001.wasm"
        assert plan.writes[path] == b"\x00asm"

    def test_an_earlier_version_keeps_its_own_file(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        library.publish(artifact, content=".b = 2\n", actor="alice")
        assert library.read_version(artifact, 1).path == "versions/0001.vrl"
        assert library.read_version(artifact, 2).path == "versions/0002.vrl"


class TestRollback:
    def test_rollback_repoints_current_and_keeps_newer_versions(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        library.publish(artifact, content=".b = 2\n", actor="alice")
        changed, plan = library.rollback(artifact, 1, VRL_SOURCE)
        assert changed is True
        assert plan.writes == {"current.vrl": VRL_SOURCE}
        summary = library.summarise("parse", artifact)
        assert summary.current == 1
        assert summary.versions == (1, 2)

    def test_rolling_back_to_the_current_version_is_a_no_op(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        changed, plan = library.rollback(artifact, 1, VRL_SOURCE)
        assert changed is False
        assert plan.writes == {}

    def test_rolling_back_to_an_absent_version_raises(self, artifact):
        with pytest.raises(library.VersionNotFoundError):
            library.rollback(artifact, 9, VRL_SOURCE)


class TestMetadata:
    def test_metadata_edits_never_produce_a_version(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        assert library.set_metadata(artifact, description="now with feeling") is True
        assert library.set_metadata(artifact, labels={"team": "network"}) is True
        assert library.set_metadata(artifact, group="beats") is True
        summary = library.summarise("parse", artifact)
        assert summary.versions == (1,)
        assert summary.current == 1
        assert summary.description == "now with feeling"
        assert summary.labels == {"team": "network"}
        assert summary.group == "beats"

    def test_an_unchanged_edit_reports_no_change(self, artifact):
        assert library.set_metadata(artifact, description="parse the envelope") is False

    def test_an_omitted_field_is_left_alone(self, artifact):
        library.set_metadata(artifact, labels={"team": "network"})
        library.set_metadata(artifact, description="other")
        assert library.summarise("parse", artifact).labels == {"team": "network"}

    def test_labels_are_cleared_by_an_empty_map(self, artifact):
        library.set_metadata(artifact, labels={"team": "network"})
        assert library.set_metadata(artifact, labels={}) is True
        assert library.summarise("parse", artifact).labels == {}

    @pytest.mark.parametrize("bad", [{"team": "a b"}, {"te am": "x"}, {"team": ""}])
    def test_unsafe_labels_are_refused(self, artifact, bad):
        with pytest.raises(library.InvalidArtifactError):
            library.set_metadata(artifact, labels=bad)

    def test_a_non_string_label_is_refused(self, artifact):
        with pytest.raises(library.InvalidArtifactError, match="must be a string"):
            library.set_metadata(artifact, labels={"team": 7})

    def test_state_moves_without_touching_content(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        assert library.set_state(artifact, library.LifecycleState.DEPRECATED) is True
        assert library.set_state(artifact, library.LifecycleState.DEPRECATED) is False
        summary = library.summarise("parse", artifact)
        assert summary.state is library.LifecycleState.DEPRECATED
        assert summary.versions == (1,)


class TestTags:
    def test_a_tag_names_a_version(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        library.publish(artifact, content=".b = 2\n", actor="alice")
        assert library.set_tag(artifact, "stable", 1) is True
        assert library.resolve_tag(artifact, "stable") == 1
        assert library.summarise("parse", artifact).tags == {"stable": 1}

    def test_publishing_does_not_move_a_tag(self, artifact):
        # Repointing is explicit, never a side effect of publishing.
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        library.set_tag(artifact, "stable", 1)
        library.publish(artifact, content=".b = 2\n", actor="alice")
        assert library.resolve_tag(artifact, "stable") == 1
        assert library.summarise("parse", artifact).current == 2

    def test_repointing_a_tag(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        library.publish(artifact, content=".b = 2\n", actor="alice")
        library.set_tag(artifact, "stable", 1)
        assert library.set_tag(artifact, "stable", 2) is True
        assert library.set_tag(artifact, "stable", 2) is False
        assert library.resolve_tag(artifact, "stable") == 2

    def test_a_tag_on_an_absent_version_is_refused(self, artifact):
        with pytest.raises(library.VersionNotFoundError):
            library.set_tag(artifact, "stable", 4)

    def test_deleting_a_tag_keeps_the_version(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        library.set_tag(artifact, "stable", 1)
        assert library.delete_tag(artifact, "stable") is True
        assert library.version_numbers(artifact) == [1]
        with pytest.raises(library.TagNotFoundError):
            library.delete_tag(artifact, "stable")

    def test_an_unsafe_tag_name_is_refused(self, artifact):
        library.publish(artifact, content=VRL_SOURCE, actor="alice")
        with pytest.raises(library.InvalidArtifactError):
            library.set_tag(artifact, "not a tag", 1)


class TestFilters:
    def _summary(self, **kwargs):
        env = library.new_artifact(library.kind("vrl"), **kwargs)
        return library.summarise(kwargs.pop("name", "parse-syslog"), env)

    def test_filters_and_together(self):
        summary = self._summary(group="network", labels={"team": "sec"}, description="syslog")
        assert library.matches(summary, kind_name="vrl", group="network") is True
        assert library.matches(summary, kind_name="vrl", group="beats") is False

    def test_a_bare_label_key_matches_any_value(self):
        summary = self._summary(labels={"team": "sec"})
        assert library.matches(summary, labels=["team"]) is True
        assert library.matches(summary, labels=["team=sec"]) is True
        assert library.matches(summary, labels=["team=net"]) is False

    def test_every_label_selector_must_match(self):
        summary = self._summary(labels={"team": "sec", "tier": "1"})
        assert library.matches(summary, labels=["team=sec", "tier=1"]) is True
        assert library.matches(summary, labels=["team=sec", "tier=2"]) is False

    def test_text_matches_name_or_description_case_insensitively(self):
        summary = self._summary(description="Parses SYSLOG")
        assert library.matches(summary, text="syslog") is True
        assert library.matches(summary, text="PARSE-SYS") is True
        assert library.matches(summary, text="netflow") is False

    def test_state_filter(self):
        summary = self._summary(state=library.LifecycleState.DEPRECATED)
        assert library.matches(summary, state="deprecated") is True
        assert library.matches(summary, state="enabled") is False


def _commit(crud, name, env, plan):
    """Write a manifest and the files its change implies, the way the router does."""
    crud.put_bundle(
        LIBRARY_CLASS,
        name,
        env,
        actor="alice",
        writes=dict(plan.writes),
        removals=list(plan.removals),
    )


class TestGitRoundTrip:
    def test_an_artefact_survives_a_commit_and_re_read_verbatim(self, crud, vrl_kind):
        env = library.new_artifact(vrl_kind, group="network")
        _v, _c, plan = library.publish(env, content=VRL_SOURCE, actor="alice", description="first")
        library.set_tag(env, "stable", 1)
        _commit(crud, "parse-syslog", env, plan)

        reloaded = crud.get(LIBRARY_CLASS, "parse-syslog")
        found = library.read_version(reloaded, 1)
        assert crud.read_payload(LIBRARY_CLASS, "parse-syslog", found.path) == VRL_SOURCE
        summary = library.summarise("parse-syslog", reloaded)
        assert summary.group == "network"
        assert summary.tags == {"stable": 1}
        assert summary.digest == library.digest_of(vrl_kind, VRL_SOURCE)

    def test_content_is_a_real_file_not_a_yaml_scalar(self, crud, vrl_kind):
        env = library.new_artifact(vrl_kind)
        _v, _c, plan = library.publish(env, content=VRL_SOURCE, actor="alice")
        _commit(crud, "parse-syslog", env, plan)
        bundle = crud.repo_path / "config" / "library" / "parse-syslog"
        assert (bundle / "versions" / "0001.vrl").read_text() == VRL_SOURCE
        assert (bundle / "current.vrl").read_text() == VRL_SOURCE
        assert VRL_SOURCE not in (bundle / "manifest.yaml").read_text()

    def test_current_follows_the_marked_version(self, crud, vrl_kind):
        env = library.new_artifact(vrl_kind)
        _v, _c, first = library.publish(env, content=VRL_SOURCE, actor="alice")
        _commit(crud, "parse-syslog", env, first)
        _v, _c, second = library.publish(env, content=".b = 2\n", actor="alice")
        _commit(crud, "parse-syslog", env, second)
        current = crud.repo_path / "config" / "library" / "parse-syslog" / "current.vrl"
        assert current.read_text() == ".b = 2\n"

        _changed, back = library.rollback(env, 1, VRL_SOURCE)
        _commit(crud, "parse-syslog", env, back)
        assert current.read_text() == VRL_SOURCE

    def test_a_bundle_is_listed_and_deleted_whole(self, crud, vrl_kind):
        env = library.new_artifact(vrl_kind)
        _v, _c, plan = library.publish(env, content=VRL_SOURCE, actor="alice")
        _commit(crud, "parse-syslog", env, plan)
        assert crud.list(LIBRARY_CLASS) == ["parse-syslog"]
        assert crud.payloads(LIBRARY_CLASS, "parse-syslog") == [
            "current.vrl",
            "versions/0001.vrl",
        ]

        crud.delete(LIBRARY_CLASS, "parse-syslog", actor="alice")
        assert crud.list(LIBRARY_CLASS) == []
        assert not (crud.repo_path / "config" / "library" / "parse-syslog" / "current.vrl").exists()

    def test_the_envelope_is_the_one_versioneddoc_reads(self, crud, vrl_kind):
        # No second versioning mechanism: the shared reader must understand what
        # the library writes.
        env = library.new_artifact(vrl_kind)
        _v, _c, first = library.publish(env, content=VRL_SOURCE, actor="alice")
        _commit(crud, "parse-syslog", env, first)
        _v, _c, second = library.publish(env, content=".b = 2\n", actor="alice")
        _commit(crud, "parse-syslog", env, second)

        versioned = VersionedDoc(crud)
        assert versioned.list_versions(LIBRARY_CLASS, "parse-syslog") == [1, 2]
        assert versioned.status(LIBRARY_CLASS, "parse-syslog") == "published"
        assert versioned.get_published(LIBRARY_CLASS, "parse-syslog")["file"] == "versions/0002.vrl"
        assert (
            versioned.get_version(LIBRARY_CLASS, "parse-syslog", 1)["file"] == "versions/0001.vrl"
        )
