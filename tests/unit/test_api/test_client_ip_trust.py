#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_client_ip_trust.py
#  Purpose:      The audited client address comes only from proxies api.forwarded_allow_ips trusts
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Which address an audit event records.

`client_ip` is only worth reading if a caller cannot choose it. A proxy appends the
address it saw to X-Forwarded-For, so behind the peers ``api.forwarded_allow_ips``
trusts, the client is the right-most entry that is not itself a trusted proxy.
Whatever an untrusted peer sends is ignored, and ``auth.trust_proxy_auth_headers``
(on in these fixtures) has no say in it.

Each test drives the real app from a chosen TCP peer and reads the event off a
real log sink.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.api.middleware.forwarded_headers import TrustedProxies
from dfe_engine.settings import APISettings, DFESettings

from .conftest import ADMIN_PASSWORD

POD_NETWORK = "10.42.0.0/16"
# The console pod making its server-side call, inside the trusted pod network.
CONSOLE_POD = ("10.42.3.17", 40312)
# A peer outside every trusted network, the shape of a direct caller.
OUTSIDER = ("198.18.5.5", 40312)
# The address the edge proxy saw and appended.
CLIENT = "203.0.113.9"
# What a caller writes into its own X-Forwarded-For to pose as someone else.
FORGED = "192.0.2.66"
# Another proxy hop inside the pod network.
INNER_HOP = "10.42.9.9"

type Headers = list[tuple[str, str]]


@contextmanager
def _engine(
    api_settings: DFESettings, peer: tuple[str, int], forwarded_allow_ips: str = POD_NETWORK
) -> Iterator[TestClient]:
    """The real app, reached from *peer*, trusting *forwarded_allow_ips*."""
    api = api_settings.api.model_copy(update={"forwarded_allow_ips": forwarded_allow_ips})
    app = create_app(settings=api_settings.model_copy(update={"api": api}))
    try:
        with TestClient(app, client=peer, raise_server_exceptions=False) as client:
            yield client
    finally:
        _registries.clear()


def _xff(*values: str) -> Headers:
    """One X-Forwarded-For header line per value."""
    return [("X-Forwarded-For", value) for value in values]


def _audited_ip(client: TestClient, audit_events: list[dict], headers: Headers) -> str | None:
    """The client_ip on the auth.login.denied event an unauthenticated request emits."""
    resp = client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 401
    denied = [e for e in audit_events if e["event"] == "auth.login.denied"]
    assert len(denied) == 1
    return denied[0]["client_ip"]


class TestUntrustedPeer:
    """A peer outside the trust list is the client, whatever it forwards."""

    def test_forged_header_is_ignored(self, api_settings: DFESettings, audit_events: list[dict]):
        with _engine(api_settings, OUTSIDER) as client:
            assert _audited_ip(client, audit_events, _xff(FORGED)) == OUTSIDER[0]

    def test_default_trusts_loopback_only(
        self, api_settings: DFESettings, audit_events: list[dict]
    ):
        """The shipped default does not trust the pod network, so the pod is recorded."""
        default = APISettings().forwarded_allow_ips
        with _engine(api_settings, CONSOLE_POD, default) as client:
            assert _audited_ip(client, audit_events, _xff(CLIENT)) == CONSOLE_POD[0]

    def test_empty_trust_list_believes_no_one(
        self, api_settings: DFESettings, audit_events: list[dict]
    ):
        with _engine(api_settings, ("127.0.0.1", 1), "") as client:
            assert _audited_ip(client, audit_events, _xff(FORGED)) == "127.0.0.1"


class TestTrustedPeer:
    """Behind a trusted peer the chain is read right to left past trusted hops."""

    def test_rightmost_untrusted_entry_is_the_client(
        self, api_settings: DFESettings, audit_events: list[dict]
    ):
        """The caller's own entry sits left of the one the edge appended, so it loses."""
        with _engine(api_settings, CONSOLE_POD) as client:
            headers = _xff(f"{FORGED}, {CLIENT}, {INNER_HOP}")
            assert _audited_ip(client, audit_events, headers) == CLIENT

    def test_all_trusted_chain_yields_the_leftmost(
        self, api_settings: DFESettings, audit_events: list[dict]
    ):
        """Every hop is a trusted proxy, so the first of them made the request."""
        with _engine(api_settings, CONSOLE_POD) as client:
            headers = _xff(f"10.42.1.1, {INNER_HOP}")
            assert _audited_ip(client, audit_events, headers) == "10.42.1.1"

    @pytest.mark.parametrize(
        "malformed",
        [
            "not-an-ip",
            "",
            "999.1.1.1",
            "10.0.0.1:port",
            "10.0.0.1:99999",
            "[2001:db8::1",
            "[2001:db8::1]443",
            "unknown",
        ],
    )
    def test_malformed_entries_are_skipped(
        self, api_settings: DFESettings, audit_events: list[dict], malformed: str
    ):
        """A right-hand entry that names no address is passed over, not recorded."""
        with _engine(api_settings, CONSOLE_POD) as client:
            headers = _xff(f"{CLIENT}, {malformed}, {INNER_HOP}")
            assert _audited_ip(client, audit_events, headers) == CLIENT

    def test_chain_with_no_address_keeps_the_peer(
        self, api_settings: DFESettings, audit_events: list[dict]
    ):
        with _engine(api_settings, CONSOLE_POD) as client:
            assert _audited_ip(client, audit_events, _xff("garbage, , unknown")) == CONSOLE_POD[0]

    @pytest.mark.parametrize(
        ("entry", "address"),
        [
            ("203.0.113.9:8443", "203.0.113.9"),
            ("[2001:db8::7]:443", "2001:db8::7"),
            ("[2001:db8::7]", "2001:db8::7"),
            ("2001:db8::7", "2001:db8::7"),
        ],
    )
    def test_port_is_not_part_of_the_address(
        self, api_settings: DFESettings, audit_events: list[dict], entry: str, address: str
    ):
        with _engine(api_settings, CONSOLE_POD) as client:
            assert _audited_ip(client, audit_events, _xff(entry)) == address

    def test_repeated_header_lines_read_as_one_chain(
        self, api_settings: DFESettings, audit_events: list[dict]
    ):
        with _engine(api_settings, CONSOLE_POD) as client:
            headers = _xff(FORGED, CLIENT, INNER_HOP)
            assert _audited_ip(client, audit_events, headers) == CLIENT

    def test_no_header_keeps_the_peer(self, api_settings: DFESettings, audit_events: list[dict]):
        with _engine(api_settings, CONSOLE_POD) as client:
            assert _audited_ip(client, audit_events, []) == CONSOLE_POD[0]

    def test_ipv4_mapped_peer_matches_an_ipv4_network(
        self, api_settings: DFESettings, audit_events: list[dict]
    ):
        """A dual-stack listener reports the pod as ::ffff:a.b.c.d."""
        with _engine(api_settings, ("::ffff:10.42.3.17", 40312)) as client:
            assert _audited_ip(client, audit_events, _xff(CLIENT)) == CLIENT

    def test_star_records_the_leftmost_entry(
        self, api_settings: DFESettings, audit_events: list[dict]
    ):
        """'*' trusts every hop, so the caller's own entry wins; the setting says so."""
        with _engine(api_settings, OUTSIDER, "*") as client:
            headers = _xff(f"{FORGED}, {CLIENT}")
            assert _audited_ip(client, audit_events, headers) == FORGED


def test_console_login_audits_the_forwarded_client(
    api_settings: DFESettings, audit_events: list[dict]
):
    """The console's server-side login forwards the browser's chain; the caller is recorded."""
    with _engine(api_settings, CONSOLE_POD) as client:
        client.app.state.account_store.reset_password("admin", ADMIN_PASSWORD)
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "not-the-password"},
            headers=_xff(f"{FORGED}, {CLIENT}"),
        )
        assert resp.status_code == 401
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
            headers=_xff(f"{FORGED}, {CLIENT}"),
        )
        assert resp.status_code == 200

    denied = [e for e in audit_events if e["event"] == "auth.login.denied"]
    succeeded = [e for e in audit_events if e["event"] == "auth.login.success"]
    assert [e["client_ip"] for e in denied] == [CLIENT]
    assert [e["client_ip"] for e in succeeded] == [CLIENT]


class TestTrustListParsing:
    """``api.forwarded_allow_ips`` is read once, at startup."""

    @pytest.mark.parametrize("value", ["10.42.0.0/16, gateway", "*, 10.42.0.0/16", "10.42.0.0/33"])
    def test_bad_entry_stops_startup(self, api_settings: DFESettings, value: str):
        api = api_settings.api.model_copy(update={"forwarded_allow_ips": value})
        with pytest.raises(ValueError, match="api.forwarded_allow_ips"):
            create_app(settings=api_settings.model_copy(update={"api": api}))

    def test_bare_address_trusts_that_address_only(self):
        trusted = TrustedProxies.parse("10.42.3.17")
        assert trusted.trusts("10.42.3.17")
        assert not trusted.trusts("10.42.3.18")

    def test_host_bits_in_a_network_are_accepted(self):
        assert TrustedProxies.parse("10.42.3.17/16").trusts("10.42.200.1")

    def test_ipv4_address_is_not_in_an_ipv6_network(self):
        trusted = TrustedProxies.parse("::/0")
        assert trusted.trusts("2001:db8::1")
        assert not trusted.trusts("10.42.3.17")

    @pytest.mark.parametrize("value", ["", " , ", "127.0.0.1"])
    def test_non_ip_peer_is_never_trusted(self, value: str):
        assert not TrustedProxies.parse(value).trusts("testclient")
