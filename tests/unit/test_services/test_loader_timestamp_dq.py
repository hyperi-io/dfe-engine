#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_loader_timestamp_dq.py
#  Purpose:      The engine takes only the timestamp_dq.invalid_action dfe-loader starts with
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-loader refuses to start on any ``invalid_action`` but ``replace_with_now``, compared exactly.

A config the engine accepted and the loader then refused would be written to the
deploy repo and leave the loader crash-looping on it.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dfe_engine.services import default_configs
from dfe_engine.services.models.loader import LoaderConfig, TimestampDqConfig
from dfe_engine.yaml_utils import yaml_load


def test_the_loader_s_own_action_is_accepted():
    assert TimestampDqConfig(invalid_action="replace_with_now").invalid_action == "replace_with_now"
    assert TimestampDqConfig().invalid_action == "replace_with_now"


@pytest.mark.parametrize("action", ["drop", "dlq", "REPLACE_WITH_NOW", "", "keep"])
def test_an_action_the_loader_refuses_is_refused(action):
    with pytest.raises(ValidationError, match="invalid_action"):
        TimestampDqConfig(invalid_action=action)


@pytest.mark.parametrize("name", ["loader-default.yaml", "loader-production.yaml"])
def test_the_shipped_loader_configs_validate(name):
    config = yaml_load(Path(default_configs.__file__).parent / name)
    assert config["timestamp_dq"]["invalid_action"] == "replace_with_now"
    LoaderConfig.model_validate(config)
