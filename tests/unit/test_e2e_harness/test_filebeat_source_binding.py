#  Project:      dfe-engine
#  File:         tests/unit/test_e2e_harness/test_filebeat_source_binding.py
#  Purpose:      The filebeat instance config the transform apps actually run on
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine's half of the WS21 wiring, pinned to what the apps consume.

The acceptance case starts with a ``filebeat`` Source and ends with rows in
ClickHouse, and the hop between the two is a transform instance whose topics
come from nothing but the source name. Both transform apps prove their own half
against a real broker in their own repos; this pins the values the engine hands
them, so a manifest edit that renames a topic fails here rather than on a
cluster.

The app-side counterparts:

- dfe-transform-vrl derives the source back out of its input topic
  (``kafka::derive_dfe_source``) and logs the round trip as its DFE topology;
  its ``tests/e2e/filebeat_kafka.rs`` runs on exactly these three values.
- dfe-transform-vector reads ``dfe_source`` natively and derives the topics and
  consumer group from it (``config/loader.rs``).
"""

from __future__ import annotations

from dfe_engine.appmgmt.catalogue import descriptor, render_source_binding

SOURCE = "filebeat"


class TestVrlBinding:
    """The app has no source shorthand, so every topic is set explicitly."""

    def test_the_input_topic_is_the_sources_land_topic(self):
        binding = render_source_binding(descriptor("dfe-transform-vrl"), SOURCE)
        assert binding["config.source.topics"] == ["filebeat_land"]

    def test_the_output_topic_is_the_sources_load_topic(self):
        binding = render_source_binding(descriptor("dfe-transform-vrl"), SOURCE)
        assert binding["config.sink.topic"] == "filebeat_load"

    def test_the_consumer_group_names_the_app_and_the_source(self):
        binding = render_source_binding(descriptor("dfe-transform-vrl"), SOURCE)
        assert binding["config.source.group_id"] == "dfe-transform-vrl-filebeat"


class TestVectorBinding:
    """The app derives its own topics, so the binding sets only the source."""

    def test_only_the_source_name_is_set(self):
        binding = render_source_binding(descriptor("dfe-transform-vector"), SOURCE)
        assert binding == {"config.dfe_source": SOURCE}


class TestBothAppsAreSourceBound:
    def test_an_instance_of_either_app_is_a_sources_transform(self):
        # The instance name IS the source, which is what makes swapping one app
        # for the other at the transform hop a like-for-like substitution.
        for service in ("dfe-transform-vrl", "dfe-transform-vector"):
            assert descriptor(service).source_bound
