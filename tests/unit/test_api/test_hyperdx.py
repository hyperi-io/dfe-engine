#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hyperdx.py
#  Purpose:      The per-org HyperDX connection endpoint resolves the caller's org
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Unit tests for the two GET /api/v1/hyperdx reads.

The handlers are called directly with real fakes (a file-backed secrets store, a
list-only org registry). ``/connection`` checks ``query:execute`` inside the
handler, so its gate is under test here; the ``/sources`` gate is a standard
require_action dependency covered elsewhere. The invariant under test for
``/connection``: a caller only ever resolves to its OWN org's credential unless a
system-scope grant says otherwise, and anything ambiguous fails closed.
For ``/sources``: an unreachable HyperDX is never reported as an empty listing.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from dfe_engine.api.v1.hyperdx import hyperdx_connection, hyperdx_identity, hyperdx_sources
from dfe_engine.auth import Scope, ScopedGrant
from dfe_engine.auth.models import AuthContext
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings


def _org(name: str, ids: list[str]) -> SimpleNamespace:
    return SimpleNamespace(name=name, org_ids=ids)


def _settings(tmp_path):
    return SimpleNamespace(
        clickhouse=SimpleNamespace(host="ch.example", port=8123, secure=False),
        secrets=SecretsSettings(provider="file", path=str(tmp_path)),
    )


def _request(orgs):
    # The scope-aware query:execute gate (moved into the handler) resolves via
    # is_action_allowed, which reads settings.auth.enabled + role_config off
    # app.state - provide both so the gate can evaluate.
    from dfe_engine.auth.roles import RoleConfig

    reg = SimpleNamespace(list=lambda: orgs)
    state = SimpleNamespace(
        org_registry=reg,
        settings=SimpleNamespace(auth=SimpleNamespace(enabled=True)),
        role_config=RoleConfig.load_builtin(),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def test_org_viewer_gets_only_its_own_org_connection(tmp_path):
    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/orgs/acme", "acme-pw")
    store.put("ch/orgs/nerk", "nerk-pw")
    user = AuthContext(user_id="u", roles=["org_viewer"], org_ids=["acme"])
    req = _request([_org("acme", ["acme"]), _org("nerk", ["nerk"])])

    conn = await hyperdx_connection(req, user, settings)

    assert conn.name == "acme"
    assert conn.username == "dfe_org_acme"
    assert conn.password == "acme-pw"
    assert conn.host == "http://ch.example:8123"


async def test_org_viewer_with_org_scoped_grant_passes_the_gate(tmp_path):
    """Regression (found by driving): an org_viewer resolves query:execute only at
    its OWN org scope (org-scoped group -> org-scoped grant). The old gate checked
    query:execute at SYSTEM scope, so every org_viewer got 403 and the embed showed
    'No available connections'. The gate must accept the org-scoped grant.
    """
    from dfe_engine.auth import Scope, ScopedGrant

    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/orgs/acme", "acme-pw")
    user = AuthContext(
        user_id="acme-viewer",
        roles=["org_viewer"],
        org_ids=["acme"],
        grants=[ScopedGrant(role="org_viewer", scope=Scope(type="org", id="acme"))],
    )
    req = _request([_org("acme", ["acme"])])

    conn = await hyperdx_connection(req, user, settings)

    assert conn.name == "acme"
    assert conn.username == "dfe_org_acme"


async def test_platform_role_gets_the_unrestricted_reader(tmp_path):
    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/service/query_reader", "qr-pw")
    user = AuthContext(user_id="u", roles=["data_analyst"], org_ids=[])
    req = _request([_org("acme", ["acme"])])

    conn = await hyperdx_connection(req, user, settings)

    assert conn.name == "platform"
    assert conn.username == "dfe_query_reader"
    assert conn.password == "qr-pw"


async def test_a_system_query_role_beside_org_viewer_reads_unrestricted(tmp_path):
    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/service/query_reader", "qr-pw")
    store.put("ch/orgs/acme", "acme-pw")
    user = AuthContext(user_id="u", roles=["org_viewer", "data_analyst"], org_ids=["acme"])
    req = _request([_org("acme", ["acme"])])

    conn = await hyperdx_connection(req, user, settings)

    assert conn.username == "dfe_query_reader"


async def test_a_system_role_that_runs_no_query_does_not_read_unrestricted(tmp_path):
    # infra_admin holds no query:execute, so only org_viewer passes the gate.
    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/service/query_reader", "qr-pw")
    store.put("ch/orgs/acme", "acme-pw")
    user = AuthContext(user_id="u", roles=["org_viewer", "infra_admin"], org_ids=["acme"])
    req = _request([_org("acme", ["acme"])])

    conn = await hyperdx_connection(req, user, settings)

    assert conn.username == "dfe_org_acme"


@pytest.mark.parametrize("other", ["dfe_operator", "infra_viewer", "data_analyst", "admin"])
async def test_an_org_scoped_caller_gets_its_org_user_whatever_else_it_holds(tmp_path, other):
    """Roles bound at one org's scope never reach the cross-org reader."""
    from dfe_engine.auth import Scope, ScopedGrant

    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/service/query_reader", "qr-pw")
    store.put("ch/orgs/acme", "acme-pw")
    acme = Scope(type="org", id="acme")
    user = AuthContext(
        user_id="acme-member",
        roles=["org_viewer", other],
        org_ids=["acme"],
        grants=[ScopedGrant(role="org_viewer", scope=acme), ScopedGrant(role=other, scope=acme)],
    )
    req = _request([_org("acme", ["acme"]), _org("nerk", ["nerk"])])

    conn = await hyperdx_connection(req, user, settings)

    assert (conn.name, conn.username, conn.password) == ("acme", "dfe_org_acme", "acme-pw")


async def test_a_system_scope_data_analyst_grant_reads_unrestricted(tmp_path):
    from dfe_engine.auth import Scope, ScopedGrant

    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/service/query_reader", "qr-pw")
    user = AuthContext(
        user_id="analyst",
        roles=["data_analyst"],
        org_ids=[],
        grants=[ScopedGrant(role="data_analyst", scope=Scope())],
    )
    req = _request([_org("acme", ["acme"])])

    conn = await hyperdx_connection(req, user, settings)

    assert (conn.name, conn.username) == ("platform", "dfe_query_reader")


async def test_a_caller_with_no_query_execute_anywhere_is_refused(tmp_path):
    from dfe_engine.auth import Scope, ScopedGrant

    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/service/query_reader", "qr-pw")
    store.put("ch/orgs/acme", "acme-pw")
    user = AuthContext(
        user_id="operator",
        roles=["dfe_operator", "infra_viewer"],
        org_ids=["acme"],
        grants=[
            ScopedGrant(role="dfe_operator", scope=Scope()),
            ScopedGrant(role="infra_viewer", scope=Scope(type="org", id="acme")),
        ],
    )
    req = _request([_org("acme", ["acme"])])

    with pytest.raises(HTTPException) as exc:
        await hyperdx_connection(req, user, settings)
    assert exc.value.status_code == 403


async def test_caller_with_no_resolvable_org_is_refused(tmp_path):
    settings = _settings(tmp_path)
    user = AuthContext(user_id="u", roles=["org_viewer"], org_ids=[])
    req = _request([_org("acme", ["acme"])])

    with pytest.raises(HTTPException) as exc:
        await hyperdx_connection(req, user, settings)
    assert exc.value.status_code == 403


async def test_caller_spanning_two_separate_orgs_fails_closed(tmp_path):
    settings = _settings(tmp_path)
    user = AuthContext(user_id="u", roles=["org_viewer"], org_ids=["acme", "nerk"])
    req = _request([_org("acme", ["acme"]), _org("nerk", ["nerk"])])

    with pytest.raises(HTTPException) as exc:
        await hyperdx_connection(req, user, settings)
    assert exc.value.status_code == 403


async def test_org_with_many_ids_still_resolves_to_one_connection(tmp_path):
    """A single org spanning many tenant ids is ONE org, not an ambiguous span."""
    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/orgs/span", "span-pw")
    user = AuthContext(user_id="u", roles=["org_viewer"], org_ids=["t1", "t2"])
    req = _request([_org("span", ["t1", "t2"])])

    conn = await hyperdx_connection(req, user, settings)

    assert conn.name == "span"
    assert conn.username == "dfe_org_span"


async def test_org_without_a_credential_is_503(tmp_path):
    settings = _settings(tmp_path)
    user = AuthContext(user_id="u", roles=["org_viewer"], org_ids=["acme"])
    req = _request([_org("acme", ["acme"])])

    with pytest.raises(HTTPException) as exc:
        await hyperdx_connection(req, user, settings)
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# hyperdx_identity: the username /auth/me reports, which names the fork's team
# ---------------------------------------------------------------------------
#
# The fork seeds a team only with a connection of the team's own name, so the two reads must agree.

_ACME = Scope(type="org", id="acme")

_GRANTED = [
    pytest.param(
        AuthContext(user_id="u", roles=["org_viewer"], org_ids=["acme"]),
        "dfe_org_acme",
        id="org-viewer",
    ),
    pytest.param(
        AuthContext(user_id="u", roles=["data_analyst"], org_ids=[]),
        "dfe_query_reader",
        id="platform-analyst",
    ),
    pytest.param(
        AuthContext(
            user_id="acme-admin",
            roles=["org_viewer", "admin"],
            org_ids=["acme"],
            grants=[
                ScopedGrant(role="org_viewer", scope=_ACME),
                ScopedGrant(role="admin", scope=_ACME),
            ],
        ),
        "dfe_org_acme",
        id="org-scoped-admin",
    ),
    pytest.param(
        AuthContext(user_id="u", roles=["org_viewer", "data_analyst"], org_ids=["acme"]),
        "dfe_query_reader",
        id="viewer-beside-platform-role",
    ),
]


@pytest.mark.parametrize(("user", "username"), _GRANTED)
async def test_the_identity_is_the_username_the_connection_hands_over(tmp_path, user, username):
    settings = _settings(tmp_path)
    store = build_secrets(settings.secrets)
    store.put("ch/service/query_reader", "qr-pw")
    store.put("ch/orgs/acme", "acme-pw")
    req = _request([_org("acme", ["acme"]), _org("nerk", ["nerk"])])

    conn = await hyperdx_connection(req, user, settings)

    assert conn.username == username
    assert hyperdx_identity(req, user) == conn.username


@pytest.mark.parametrize(
    "user",
    [
        pytest.param(
            AuthContext(
                user_id="operator",
                roles=["dfe_operator"],
                org_ids=[],
                grants=[ScopedGrant(role="dfe_operator", scope=Scope())],
            ),
            id="no-query-execute",
        ),
        pytest.param(AuthContext(user_id="u", roles=["org_viewer"], org_ids=[]), id="no-org"),
        pytest.param(
            AuthContext(user_id="u", roles=["org_viewer"], org_ids=["acme", "nerk"]),
            id="two-orgs",
        ),
        pytest.param(AuthContext(user_id="nobody"), id="no-roles"),
    ],
)
async def test_a_caller_the_connection_refuses_has_no_identity(tmp_path, user):
    settings = _settings(tmp_path)
    req = _request([_org("acme", ["acme"]), _org("nerk", ["nerk"])])

    with pytest.raises(HTTPException) as exc:
        await hyperdx_connection(req, user, settings)

    assert exc.value.status_code == 403
    assert hyperdx_identity(req, user) == ""


def test_an_org_with_no_credential_yet_still_has_an_identity():
    """Named without the secret, so the fork creates the team and retries the seed."""
    user = AuthContext(user_id="u", roles=["org_viewer"], org_ids=["acme"])

    assert hyperdx_identity(_request([_org("acme", ["acme"])]), user) == "dfe_org_acme"


# ---------------------------------------------------------------------------
# GET /api/v1/hyperdx/sources
# ---------------------------------------------------------------------------


class FakeFork:
    """The fork's GET /dfe/sources, or an unreachable one."""

    def __init__(self, teams: list[dict] | None) -> None:
        self._teams = teams

    async def list_dfe_sources(self):
        return None if self._teams is None else {"teams": self._teams}


def _sources_request(client):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(hyperdx_client=client)))


async def test_sources_reports_the_team_each_source_landed_on():
    client = FakeFork(
        [
            {
                "team": "t1",
                "teamName": "dfe-admins",
                "sources": [
                    {
                        "id": "s1",
                        "name": "filebeat",
                        "from": {"databaseName": "dfe", "tableName": "filebeat"},
                    }
                ],
            },
            {"team": "t2", "teamName": "customer-acme", "sources": []},
        ]
    )

    listing = await hyperdx_sources(_sources_request(client))

    assert [team.team_name for team in listing.teams] == ["dfe-admins", "customer-acme"]
    assert listing.teams[0].sources[0].name == "filebeat"
    assert listing.teams[0].sources[0].table == {
        "databaseName": "dfe",
        "tableName": "filebeat",
    }


async def test_sources_is_503_when_hyperdx_does_not_answer():
    # An empty listing reads as "the source is missing", a different fault.
    with pytest.raises(HTTPException) as exc:
        await hyperdx_sources(_sources_request(FakeFork(None)))
    assert exc.value.status_code == 503


async def test_sources_is_503_on_a_deployment_without_hyperdx():
    with pytest.raises(HTTPException) as exc:
        await hyperdx_sources(_sources_request(None))
    assert exc.value.status_code == 503
