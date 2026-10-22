#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sigma_org_reads_off_loop.py
#  Purpose:      The sigma propagation routes read the org registry off the event loop
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A stalled org registry read holds up only the sigma request waiting on it.

Propagation checks the orgs a hunt is for against the registry, and builds its propagator
from the orgs the registry holds. Each case stalls one of those reads, then asks ``/livez``
for an answer while it is stalled. A route that read on the event loop holds ``/livez`` up
with it, as it would every request and the probes.
"""

import pytest

from tests.support.stalled_call import Setup, Stall, registry_call, request_while_stalled
from tests.unit.test_api.test_sigma_propagate import _BINDING, _wire_gitcrud

pytestmark = pytest.mark.usefixtures("customer_orgs")

SIGMA = "/api/v1/sigma"
_PROPAGATE = {"hunt_customers": ["acme"]}


@pytest.fixture
def sigma_enabled() -> bool:
    """The sigma routes mount only on a deployment that enables them."""
    return True


# What stalls, and the request that waits on it. Propagate reads the registry twice: to check
# its hunt customers, then to build its propagator.
_CASES: dict[str, tuple[Setup, str, str, dict | None]] = {
    "propagate's customer check": (registry_call("list"), "POST", f"{SIGMA}/propagate", _PROPAGATE),
    "propagate's propagator": (
        registry_call("list", skip=1),
        "POST",
        f"{SIGMA}/propagate",
        _PROPAGATE,
    ),
    "list bindings": (registry_call("list"), "GET", f"{SIGMA}/bindings", None),
    "get a binding": (registry_call("list"), "GET", f"{SIGMA}/bindings/{_BINDING}", None),
    "delete a binding": (registry_call("list"), "DELETE", f"{SIGMA}/bindings/{_BINDING}", None),
}


@pytest.mark.parametrize("case", list(_CASES))
def test_an_org_read_holds_up_only_its_own_sigma_request(
    case, client, app, admin_headers, tmp_path, monkeypatch
):
    setup, method, path, body = _CASES[case]
    _wire_gitcrud(app, tmp_path)
    stall = Stall()
    setup(app, stall, monkeypatch)

    done = request_while_stalled(
        client, stall, case, method, path, json=body, headers=admin_headers
    )

    assert done.status_code < 500, done.text
