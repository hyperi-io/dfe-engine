#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_files.py
#  Purpose:      Tests for the consumed-files surface carried in the values overlay
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Consumed-file CRUD, and the verbatim round trip through a real git repo."""

import dataclasses

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
        files.list_files({"fileSets": {"transforms": {"files": {"a.vrl": "x"}}}}, vrl_set)


# ── a set the app names table by table ────────────────────────


@pytest.fixture
def tables():
    return catalogue.file_set(VRL, "enrichment")


def _entries(doc: dict) -> list:
    return doc.get("config", {}).get("enrichment_tables")


def test_a_written_table_is_named_where_the_chart_mounts_it(tables):
    # The config file takes no template, so nothing downstream derives the entry.
    doc: dict = {}
    assert files.upsert_file(doc, tables, "geo.csv", "ip,cc\n") is True
    assert _entries(doc) == [{"name": "geo", "path": f"{tables.mount_path}/geo.csv"}]


def test_a_table_the_config_already_names_keeps_the_author_s_entry(tables):
    # The author's entry carries key columns a derived one cannot.
    authored = {"name": "geo", "path": "/srv/geo.csv", "key_columns": ["ip"]}
    doc: dict = {"config": {"enrichment_tables": [authored]}}
    files.upsert_file(doc, tables, "geo.csv", "ip,cc\n")
    assert _entries(doc) == [authored]


def test_a_deleted_table_takes_its_derived_entry_with_it(tables):
    # The app fails to load a table whose file is gone.
    doc: dict = {}
    files.upsert_file(doc, tables, "geo.csv", "ip,cc\n")
    files.upsert_file(doc, tables, "asn.csv", "ip,asn\n")
    files.delete_file(doc, tables, "geo.csv")
    assert _entries(doc) == [{"name": "asn", "path": f"{tables.mount_path}/asn.csv"}]


def test_a_deleted_table_leaves_the_author_s_entry(tables):
    authored = {"name": "geo", "path": f"{tables.mount_path}/geo.csv", "key_columns": ["ip"]}
    doc: dict = {"config": {"enrichment_tables": [authored]}}
    files.upsert_file(doc, tables, "geo.csv", "ip,cc\n")
    files.delete_file(doc, tables, "geo.csv")
    assert _entries(doc) == [authored]


def test_an_entry_naming_a_file_elsewhere_survives_every_write(tables):
    elsewhere = {"name": "tz", "path": "/etc/shared/tz.csv"}
    doc: dict = {"config": {"enrichment_tables": [elsewhere]}}
    files.upsert_file(doc, tables, "geo.csv", "ip,cc\n")
    files.delete_file(doc, tables, "geo.csv")
    assert _entries(doc) == [elsewhere]


def test_an_unchanged_file_with_no_entry_still_reports_a_change(tables):
    # A set carried over from another key has its files but none of its entries.
    doc: dict = {"fileSets": {"enrichment": {"files": [{"name": "geo.csv", "content": "a\n"}]}}}
    assert files.upsert_file(doc, tables, "geo.csv", "a\n") is True
    assert _entries(doc) == [{"name": "geo", "path": f"{tables.mount_path}/geo.csv"}]
    assert files.upsert_file(doc, tables, "geo.csv", "a\n") is False


def test_a_malformed_entry_list_is_refused_rather_than_replaced(tables):
    doc: dict = {"config": {"enrichment_tables": {"geo": "/x"}}}
    with pytest.raises(ValueError, match="expected a list"):
        files.upsert_file(doc, tables, "geo.csv", "a\n")


def test_a_set_read_as_a_directory_writes_no_entries(vrl_set):
    doc: dict = {}
    files.upsert_file(doc, vrl_set, "000_parse.vrl", VRL_SOURCE)
    assert "config" not in doc


def test_a_set_whose_mount_the_manifest_does_not_name_writes_no_entries(tables):
    # The chart that mounts it derives the entries, as the dfe-common charts do.
    unmounted = dataclasses.replace(tables, mount_path="")
    doc: dict = {}
    assert files.upsert_file(doc, unmounted, "geo.csv", "ip,cc\n") is True
    assert "config" not in doc


def test_vector_s_tables_are_named_by_its_transforms_not_its_config():
    # dfe-transform-vector reads enrichment_tables from the Vector config its
    # transform files assemble; its own config file has no such key.
    doc: dict = {}
    files.upsert_file(doc, catalogue.file_set(VECTOR, "enrichment"), "timezones.csv", "a\n")
    assert "config" not in doc
