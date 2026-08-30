#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_files.py
#  Purpose:      Tests for the consumed-files surface carried in the values overlay
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Consumed-file CRUD, and the verbatim round trip through a real git repo."""

from __future__ import annotations

import pytest

from dfe_engine.appmgmt import catalogue, files, instances
from dfe_engine.appmgmt.files import FileNotInSetError, InvalidFilenameError
from dfe_engine.appmgmt.instances import HELMVARS_CLASS

VRL = "dfe-transform-vrl"
VECTOR = "dfe-transform-vector"

VRL_SOURCE = """\
# parse the envelope, then stamp the load time
. = parse_json!(.message)
.ts = to_timestamp!(.timestamp)
if exists(.user.name) {
    .user.name = downcase(string!(.user.name))
}
"""


@pytest.fixture
def vrl_set():
    return catalogue.file_set(VRL, "transforms")


def test_empty_overlay_has_no_files(vrl_set):
    assert files.list_files({}, vrl_set) == []


def test_upsert_then_read(vrl_set):
    doc: dict = {}
    assert files.upsert_file(doc, vrl_set, "000_parse.vrl", VRL_SOURCE) is True
    found = files.read_file(doc, vrl_set, "000_parse.vrl")
    assert found.content == VRL_SOURCE
    assert found.language == "vrl"
    assert found.size_bytes == len(VRL_SOURCE.encode("utf-8"))


def test_upsert_is_idempotent(vrl_set):
    doc: dict = {}
    files.upsert_file(doc, vrl_set, "000_parse.vrl", VRL_SOURCE)
    assert files.upsert_file(doc, vrl_set, "000_parse.vrl", VRL_SOURCE) is False


def test_upsert_replaces_existing_content(vrl_set):
    doc: dict = {}
    files.upsert_file(doc, vrl_set, "000_parse.vrl", VRL_SOURCE)
    assert files.upsert_file(doc, vrl_set, "000_parse.vrl", ".x = 1\n") is True
    assert files.read_file(doc, vrl_set, "000_parse.vrl").content == ".x = 1\n"
    assert len(files.list_files(doc, vrl_set)) == 1


def test_delete_removes_only_the_named_file(vrl_set):
    doc: dict = {}
    files.upsert_file(doc, vrl_set, "000_parse.vrl", ".a = 1\n")
    files.upsert_file(doc, vrl_set, "010_enrich.vrl", ".b = 2\n")
    files.delete_file(doc, vrl_set, "000_parse.vrl")
    assert [f.name for f in files.list_files(doc, vrl_set)] == ["010_enrich.vrl"]


def test_delete_unknown_file_raises(vrl_set):
    with pytest.raises(FileNotInSetError):
        files.delete_file({}, vrl_set, "nope.vrl")


def test_read_unknown_file_raises(vrl_set):
    with pytest.raises(FileNotInSetError):
        files.read_file({}, vrl_set, "nope.vrl")


@pytest.mark.parametrize(
    "bad",
    ["../escape.vrl", "sub/dir.vrl", "..", ".hidden.vrl", "", "a b.vrl"],
)
def test_unsafe_filenames_are_refused(vrl_set, bad):
    with pytest.raises(InvalidFilenameError):
        files.upsert_file({}, vrl_set, bad, ".a = 1\n")


def test_wrong_extension_is_refused(vrl_set):
    # dfe-transform-vrl reads only *.vrl from its transforms directory, so a file
    # it would silently ignore must not be accepted here.
    with pytest.raises(InvalidFilenameError):
        files.upsert_file({}, vrl_set, "notes.txt", "hello\n")


def test_vector_accepts_yaml_not_vrl():
    vector_set = catalogue.file_set(VECTOR, "transforms")
    doc: dict = {}
    files.upsert_file(doc, vector_set, "enrich.yaml", "type: remap\n")
    with pytest.raises(InvalidFilenameError):
        files.upsert_file(doc, vector_set, "enrich.vrl", ".a = 1\n")


def test_elastic_has_no_file_sets():
    # dfe-transform-elastic selects a compiled-in transform; it reads no files.
    assert catalogue.descriptor("dfe-transform-elastic").files == ()
    with pytest.raises(catalogue.UnknownAppError):
        catalogue.file_set("dfe-transform-elastic", "transforms")


def test_content_survives_a_real_git_round_trip_verbatim(crud, vrl_set):
    # The whole storage decision rests on this: content lives inside the values
    # overlay, so it must come back byte-identical after a commit and re-read.
    app = instances.instance_of(VRL, "edge")
    doc = instances.initial_overlay(app)
    files.upsert_file(doc, vrl_set, "000_parse.vrl", VRL_SOURCE)
    crud.put(HELMVARS_CLASS, app.overlay_name, doc, actor="alice")

    reloaded = crud.get(HELMVARS_CLASS, app.overlay_name)
    assert files.read_file(reloaded, vrl_set, "000_parse.vrl").content == VRL_SOURCE


def test_committed_yaml_uses_a_block_scalar(crud, vrl_set):
    # A quoted one-liner full of \\n escapes would be unreadable in a diff and
    # unusable as a file body once the chart renders it into a ConfigMap.
    app = instances.instance_of(VRL, "edge")
    doc = instances.initial_overlay(app)
    files.upsert_file(doc, vrl_set, "000_parse.vrl", VRL_SOURCE)
    crud.put(HELMVARS_CLASS, app.overlay_name, doc, actor="alice")

    on_disk = (crud.repo_path / "values" / f"{app.overlay_name}.yaml").read_text()
    assert "content: |" in on_disk
    assert "\\n" not in on_disk


def test_a_malformed_file_set_value_is_reported(vrl_set):
    with pytest.raises(ValueError, match="expected a list"):
        files.list_files({"transformFiles": {"a.vrl": "x"}}, vrl_set)
