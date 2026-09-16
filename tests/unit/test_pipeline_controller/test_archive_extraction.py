#  Project:      dfe-engine
#  File:         tests/unit/test_pipeline/test_archive_extraction.py
#  Purpose:      A downloaded artefact archive cannot write outside its destination
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The member-path check the template extraction runs before it writes anything.

``zipfile`` does not refuse a member that resolves outside the destination -- it
rewrites the name and extracts it anyway -- so the refusal is asserted here
rather than assumed from the library.
"""

from __future__ import annotations

import re

import pytest

from dfe_engine.pipeline.pipeline_controller import reject_escaping_members


def test_a_member_inside_the_destination_is_accepted(tmp_path):
    reject_escaping_members(["templates/ingest.yaml", "nested/deep/file.json"], tmp_path)


def test_a_parent_traversal_member_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="resolves outside the destination"):
        reject_escaping_members(["../../etc/cron.d/payload"], tmp_path)


def test_an_absolute_member_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="resolves outside the destination"):
        reject_escaping_members(["/etc/cron.d/payload"], tmp_path)


def test_a_traversal_hidden_mid_path_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="resolves outside the destination"):
        reject_escaping_members(["templates/../../outside.yaml"], tmp_path)


def test_a_traversal_that_lands_back_inside_is_accepted(tmp_path):
    reject_escaping_members(["templates/../ingest.yaml"], tmp_path)


def test_the_first_escaping_member_names_itself(tmp_path):
    with pytest.raises(RuntimeError, match=re.escape("'../outside.yaml'")):
        reject_escaping_members(["ok.yaml", "../outside.yaml"], tmp_path)


def test_the_destination_itself_is_not_an_escape(tmp_path):
    reject_escaping_members([""], tmp_path)
