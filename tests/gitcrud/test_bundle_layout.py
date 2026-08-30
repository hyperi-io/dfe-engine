#  Project:      dfe-engine
#  File:         tests/gitcrud/test_bundle_layout.py
#  Purpose:      Tests for the BUNDLE resource layout (directory per resource)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A bundle class is a generic gitcrud capability, tested without the library.

The library is its first consumer, not its definition: anything storing authored
content declares ``layout=BUNDLE`` and gets the same manifest-plus-files shape.
"""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import (
    GitCrud,
    Layout,
    ResourceClass,
    ResourceClassRegistry,
    ResourceNotFoundError,
    UnsafePathError,
)
from dfe_engine.gitops.repo import GitopsRepo


@pytest.fixture
def crud(tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    registry = ResourceClassRegistry(
        [
            ResourceClass("helmvars", "values", rbac_prefix="helmvars"),
            ResourceClass("blobs", "config/blobs", rbac_prefix="blobs", layout=Layout.BUNDLE),
        ]
    )
    return GitCrud(repo, registry)


def _bundle(crud, name="thing"):
    crud.put_bundle(
        "blobs",
        name,
        {"kind": "text", "current": 1},
        actor="alice",
        writes={"versions/0001.txt": "hello\n", "current.txt": "hello\n"},
    )


class TestLayout:
    def test_the_manifest_and_its_files_land_together(self, crud):
        _bundle(crud)
        base = crud.repo_path / "config" / "blobs" / "thing"
        assert (base / "manifest.yaml").is_file()
        assert (base / "versions" / "0001.txt").read_text() == "hello\n"
        assert (base / "current.txt").read_text() == "hello\n"

    def test_one_commit_covers_the_whole_change(self, crud):
        result = crud.put_bundle(
            "blobs",
            "thing",
            {"current": 1},
            actor="alice",
            writes={"versions/0001.txt": "a\n", "current.txt": "a\n"},
        )
        assert result.changed is True
        assert len(result.files) == 3

    def test_get_reads_the_manifest(self, crud):
        _bundle(crud)
        assert crud.get("blobs", "thing")["kind"] == "text"

    def test_list_reports_directories_holding_a_manifest(self, crud):
        _bundle(crud, "alpha")
        _bundle(crud, "beta")
        assert crud.list("blobs") == ["alpha", "beta"]

    def test_a_directory_without_a_manifest_is_not_a_resource(self, crud):
        _bundle(crud)
        stray = crud.repo_path / "config" / "blobs" / "half-written"
        stray.mkdir(parents=True)
        (stray / "current.txt").write_text("orphan\n")
        assert crud.list("blobs") == ["thing"]

    def test_payloads_lists_every_file_but_the_manifest(self, crud):
        _bundle(crud)
        assert crud.payloads("blobs", "thing") == ["current.txt", "versions/0001.txt"]

    def test_read_payload_round_trips(self, crud):
        _bundle(crud)
        assert crud.read_payload("blobs", "thing", "versions/0001.txt") == "hello\n"

    def test_deleting_takes_the_payload_files_with_it(self, crud):
        _bundle(crud)
        crud.delete("blobs", "thing", actor="alice")
        assert crud.list("blobs") == []
        base = crud.repo_path / "config" / "blobs" / "thing"
        assert not (base / "versions" / "0001.txt").exists()
        assert not (base / "manifest.yaml").exists()

    def test_removals_drop_a_payload_without_touching_the_rest(self, crud):
        _bundle(crud)
        crud.put_bundle(
            "blobs", "thing", {"current": None}, actor="alice", removals=["versions/0001.txt"]
        )
        assert crud.payloads("blobs", "thing") == ["current.txt"]


class TestBinary:
    def test_bytes_are_written_verbatim(self, crud):
        payload = b"\x00asm\x01\x00\x00\x00"
        crud.put_bundle(
            "blobs", "mod", {"kind": "wasm"}, actor="alice", writes={"versions/0001.wasm": payload}
        )
        assert crud.read_payload_bytes("blobs", "mod", "versions/0001.wasm") == payload

    def test_crlf_in_bytes_is_not_rewritten(self, crud):
        # Text writes normalise to LF; a binary payload must not be touched.
        crud.put_bundle(
            "blobs",
            "mod",
            {"kind": "wasm"},
            actor="alice",
            writes={"versions/0001.wasm": b"a\r\nb"},
        )
        assert crud.read_payload_bytes("blobs", "mod", "versions/0001.wasm") == b"a\r\nb"


class TestPathSafety:
    @pytest.mark.parametrize(
        "bad",
        [
            "../escape.txt",
            "../../etc/passwd",
            "versions/../../escape.txt",
            "/absolute.txt",
            "windows\\path.txt",
            " leading.txt",
            "",
        ],
    )
    def test_a_payload_path_may_not_escape_the_bundle(self, crud, bad):
        with pytest.raises(UnsafePathError):
            crud.put_bundle("blobs", "thing", {}, actor="alice", writes={bad: "x"})

    def test_the_manifest_cannot_be_written_as_a_payload(self, crud):
        # Otherwise a payload write would silently overwrite the document the
        # same commit is writing.
        with pytest.raises(UnsafePathError):
            crud.put_bundle("blobs", "thing", {}, actor="alice", writes={"manifest.yaml": "x: 1"})

    def test_a_nested_file_named_like_the_manifest_is_fine(self, crud):
        crud.put_bundle(
            "blobs", "thing", {}, actor="alice", writes={"versions/manifest.yaml": "x: 1"}
        )
        assert crud.payloads("blobs", "thing") == ["versions/manifest.yaml"]

    def test_an_unsafe_resource_name_is_still_refused(self, crud):
        from dfe_engine.gitcrud.commit_policy import CommitPolicyError

        with pytest.raises(CommitPolicyError):
            crud.put_bundle("blobs", "../escape", {}, actor="alice")


class TestClassSeparation:
    def test_a_file_class_refuses_bundle_operations(self, crud):
        # helmvars must stay one values file: Argo and Helm read it as one.
        with pytest.raises(ValueError, match="not a bundle"):
            crud.payloads("helmvars", "anything")

    def test_a_file_class_is_unaffected(self, crud):
        crud.put("helmvars", "receiver", {"replicaCount": 2}, actor="alice")
        assert crud.list("helmvars") == ["receiver"]
        assert (crud.repo_path / "values" / "receiver.yaml").is_file()

    def test_payloads_on_an_absent_bundle_raises(self, crud):
        with pytest.raises(ResourceNotFoundError):
            crud.payloads("blobs", "nope")

    def test_read_payload_on_an_absent_file_raises(self, crud):
        _bundle(crud)
        with pytest.raises(ResourceNotFoundError):
            crud.read_payload("blobs", "thing", "versions/0009.txt")
