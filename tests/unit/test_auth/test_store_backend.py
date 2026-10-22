#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_store_backend.py
#  Purpose:      Choosing the users and groups backend at boot
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A forced document store with nowhere to connect names the setting that is missing."""

import pytest

from dfe_engine.auth.store_backend import resolve_store_backend

_TIMEOUTS = {"server_selection_timeout": 5.0, "socket_timeout": 30.0}


@pytest.mark.parametrize("requested", ["document", "Document"])
def test_a_forced_document_store_without_a_uri_names_the_setting(requested):
    with pytest.raises(ValueError) as refused:
        resolve_store_backend(requested, "", "dfe_engine", **_TIMEOUTS)

    assert "DFE_AUTH_ACCOUNTS_STORE_URI" in str(refused.value)
    assert "auth.accounts_store.uri" in str(refused.value)


def test_auto_without_a_uri_still_falls_back_to_yaml():
    backend = resolve_store_backend("auto", "", "dfe_engine", **_TIMEOUTS)

    assert backend == ("yaml", None)


def test_yaml_needs_no_uri():
    backend = resolve_store_backend("yaml", "", "dfe_engine", **_TIMEOUTS)

    assert backend == ("yaml", None)
