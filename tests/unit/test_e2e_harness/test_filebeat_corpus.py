#  Project:      dfe-engine
#  File:         tests/unit/test_e2e_harness/test_filebeat_corpus.py
#  Purpose:      Tests for the filebeat corpus loader and receiver wrapper
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The harness runs before any cluster does, so its own shapes are tested here.

A harness that wraps events wrongly fails the live run for a reason that has
nothing to do with the pipeline, which is the most expensive kind of failure to
diagnose on a shared cluster.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e import filebeat_corpus as corpus

CASES = """\
- name: umb-one
  module: umbrella
  line: "2026-01-01,a,b"
  expect: {event: {action: dns-request-Allowed}}
- name: umb-two
  module: umbrella
  line: "2026-01-02,c,d"
  expect: {event: {action: dns-request-Blocked}}
- name: ios-one
  module: ios
  line: "<134>1 Jan  1 00:00:00 host %SYS-5-CONFIG_I: x"
  expect: {}
- name: meraki-one
  module: meraki
  line: "<134>1 1500000000 MX flows src=1.2.3.4"
  expect: {}
"""


@pytest.fixture
def archive(tmp_path):
    """A miniature corpus with the shipped file's layout."""
    path = tmp_path / "cases.yaml"
    path.write_text(CASES, encoding="utf-8")
    return path


class TestLoading:
    def test_every_module_is_read(self, archive):
        found = {s.module for s in corpus.samples(archive)}
        assert found == {"cisco_umbrella", "cisco_ios", "cisco_meraki"}

    def test_the_limit_is_per_module_not_overall(self, archive):
        # A global cap would stop inside the first module and never exercise the
        # ios or meraki branches.
        found = corpus.samples(archive, limit=1)
        assert {s.module for s in found} == {"cisco_umbrella", "cisco_ios", "cisco_meraki"}
        assert len(found) == 3

    def test_goldens_are_keyed_by_their_sample_file(self, archive):
        sample = corpus.samples(archive, modules=("cisco_umbrella",))[0]
        golden = corpus.goldens(archive)[f"{sample.name}-expected.json"]
        assert golden["expected"] == [{"event": {"action": "dns-request-Allowed"}}]

    def test_a_selected_module_excludes_the_others(self, archive):
        found = corpus.samples(archive, modules=("cisco_umbrella",))
        assert {s.module for s in found} == {"cisco_umbrella"}
        assert set(corpus.goldens(archive, modules=("cisco_umbrella",))) == {
            "umb-one.log-expected.json",
            "umb-two.log-expected.json",
        }


class TestWrapping:
    def test_the_body_carries_the_line_as_message(self, archive):
        sample = corpus.samples(archive)[0]
        assert corpus.wrap(sample)["message"] == sample.line

    def test_the_discriminator_is_the_field_the_receiver_routes_on(self, archive):
        # A Source declaring match {field: _source, equals: filebeat} compiles to
        # a rule on exactly this field, so the harness must set it.
        body = corpus.wrap(corpus.samples(archive)[0])
        assert body["_source"] == "filebeat"

    def test_a_different_source_routes_somewhere_else(self, archive):
        body = corpus.wrap(corpus.samples(archive)[0], source="winlogbeat")
        assert body["_source"] == "winlogbeat"

    def test_the_marker_identifies_one_line_uniquely(self, archive):
        markers = [s.marker for s in corpus.samples(archive)]
        assert len(markers) == len(set(markers))

    def test_a_run_tag_is_carried_when_given(self, archive):
        body = corpus.wrap(corpus.samples(archive)[0], run="run-1")
        assert "e2e_run:run-1" in body["tags"]

    def test_no_run_tag_when_not_given(self, archive):
        tags = corpus.wrap(corpus.samples(archive)[0])["tags"]
        assert not any(t.startswith("e2e_run:") for t in tags)

    def test_tags_is_a_list_of_strings(self, archive):
        # The bundled VRL reads tags with includes(array!(.tags), ...). A map
        # reaches assert!(false, "contains only works on strings and array") and
        # aborts the program, so every corpus event would error out untransformed.
        tags = corpus.wrap(corpus.samples(archive)[0], run="run-1")["tags"]
        assert isinstance(tags, list)
        assert all(isinstance(t, str) for t in tags)

    def test_the_body_is_json_serialisable(self, archive):
        # It is POSTed as JSON, so a shape the encoder refuses fails the run.
        for body in corpus.wrap_all(corpus.samples(archive)):
            json.loads(json.dumps(body))

    def test_wrap_all_preserves_corpus_order(self, archive):
        found = corpus.samples(archive)
        wrapped = corpus.wrap_all(found)
        assert [b["message"] for b in wrapped] == [s.line for s in found]


class TestTheShippedCorpus:
    """Against the file the e2e suites read."""

    def test_every_module_has_samples(self):
        by_module: dict[str, int] = {}
        for sample in corpus.samples():
            by_module[sample.module] = by_module.get(sample.module, 0) + 1
        assert set(by_module) == set(corpus.MODULES)
        assert all(count > 0 for count in by_module.values())

    def test_the_umbrella_subset_needs_no_enrichment_table(self):
        found = corpus.samples(modules=(corpus.NO_ENRICHMENT_MODULE,))
        assert found, "the no-enrichment subset must not be empty"

    def test_every_module_has_a_dataset(self):
        assert set(corpus.DATASETS) == set(corpus.MODULES)

    def test_every_sample_has_an_expectation(self):
        expected = corpus.goldens()
        assert all(f"{s.name}-expected.json" in expected for s in corpus.samples())
