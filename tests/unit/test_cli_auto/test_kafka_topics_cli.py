#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_kafka_topics_cli.py
#  Purpose:      Tests for how the local `dfe kafka` commands meet the generated ones
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``dfe kafka`` is half generated and half local (dfe-engine#97).

Topic CRUD is the governed engine surface (``api/v1/kafka_topics.py``), so
``dfe kafka topics`` is generated from the OpenAPI spec like every other API
family. ``client-config`` and ``lifecycle`` reach no engine at all and stay
hand-written here.

Both halves want the name ``kafka``, and ``attach_kafka`` runs AFTER the tree is
generated: a plain ``add_command`` would replace the generated group and drop the
whole governed topic surface. What is pinned here is that it merges instead.
"""

from __future__ import annotations

import click

from dfe_engine.cli.auto.kafka import attach_kafka, kafka_group


def _root() -> click.Group:
    return click.Group(name="dfe")


def _generated_kafka(root: click.Group) -> click.Group:
    """Stand in for what ``build_command_tree`` makes from the spec's kafka routes."""
    generated = click.Group(name="kafka")
    topics = click.Group(name="topics")
    topics.add_command(click.Command(name="list"))
    topics.add_command(click.Command(name="ensure"))
    generated.add_command(topics)
    root.add_command(generated)
    return generated


class TestTheLocalGroupMergesWithTheGeneratedOne:
    def test_the_governed_topic_surface_survives(self):
        root = _root()
        _generated_kafka(root)

        attach_kafka(root)

        topics = root.commands["kafka"].commands["topics"]
        assert sorted(topics.commands) == ["ensure", "list"]

    def test_the_local_commands_land_beside_it(self):
        root = _root()
        _generated_kafka(root)

        attach_kafka(root)

        assert "client-config" in root.commands["kafka"].commands

    def test_a_generated_name_is_never_overwritten(self):
        """The governed route wins: it carries RBAC and an audit line, the local one does not."""
        root = _root()
        generated = _generated_kafka(root)
        marker = click.Command(name="client-config")
        generated.add_command(marker)

        attach_kafka(root)

        assert root.commands["kafka"].commands["client-config"] is marker

    def test_it_still_mounts_when_the_spec_generated_nothing(self):
        """An offline CLI build has no spec, so the local group is all there is."""
        root = _root()

        attach_kafka(root)

        assert root.commands["kafka"] is kafka_group


class TestTheLocalGroupOwnsNoTopicCrud:
    def test_topic_crud_is_not_hand_written_here(self):
        # A local duplicate would shadow nothing (the merge favours the generated
        # name) and would reach the broker with no RBAC and no audit line.
        assert "topics" not in kafka_group.commands
