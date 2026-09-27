#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_transform_offset_reset.py
#  Purpose:      A new transform source's consumer group starts at the log start
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A new transform source's consumer group starts at the log start.

The group is born with the source, so its first join on the log end skips every
record already on the topic. The transform charts render the log start, and a
value the engine writes into an overlay's ``config`` beats the chart's.
"""

import json

import pytest

from dfe_engine.services.models.transform_vrl import TransformVrlConfig
from dfe_engine.services.templates import generate_template

PROFILES = ["default", "production", "k8s"]


class TestTransformVrl:
    def test_the_model_starts_at_the_log_start(self):
        assert TransformVrlConfig().source.auto_offset_reset == "earliest"

    @pytest.mark.parametrize("profile", PROFILES)
    def test_every_emitted_template_starts_there(self, profile: str):
        emitted = generate_template("transform-vrl", profile=profile)
        assert emitted["source"]["auto_offset_reset"] == "earliest"

    def test_an_operator_value_is_kept(self):
        config = TransformVrlConfig.model_validate({"source": {"auto_offset_reset": "latest"}})
        assert config.source.auto_offset_reset == "latest"


class TestTransformVector:
    """The mirror carries no offset key, so the chart's ``smallest`` stands."""

    @pytest.mark.parametrize("profile", PROFILES)
    def test_no_emitted_template_sets_one(self, profile: str):
        emitted = generate_template("transform-vector", profile=profile)
        assert "auto_offset_reset" not in json.dumps(emitted)
