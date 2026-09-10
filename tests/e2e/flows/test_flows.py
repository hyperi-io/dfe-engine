#  Project:      dfe-engine
#  File:         tests/e2e/flows/test_flows.py
#  Purpose:      Every flow shape, on every transport the deployment must carry
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One source definition in, a row in the right table out - or the refusal, said plainly.

Each case creates its shape's sources through the engine API with the transport
set, waits for the reconcile to reach the running apps, sends or waits for the
records, and asserts where they landed AND where they did not. Then it deletes
what it made and asserts the instance went with it.

A deployment carries ONE transport, so a run asked for both proves a landing on
the one it carries and a refusal on the other. Which is which is read off the
deployment, not assumed from the transport the run was started with.

Waiting is on the outcome, never on a duration: a routing change reaches a pod
when Argo next polls, so the suite probes with a throwaway marker until the
routing is live, and only then sends the payload it asserts on. That ordering is
what makes "and nothing took the catch-all" mean something - an early probe row
in the catch-all table is the wait, not a failure.

The catch-all's names are the DEPLOYMENT's, not the fixture's: the label off the
receiver's compiled routing and the table off the loader's, read once, so the
suite holds either side of the rename and fails when only one side moved.

A batched POST is a pair of cases of its own rather than a shape: a shape sends
one request per record, and what a batch has to prove is that ONE request
carrying N events becomes N rows rather than 202 and nothing.

Skipped cases are governed: conftest fails the run on any skip a fixture did not
declare. See docs/data-plane/source-flow.md for the shape this proves.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.e2e.conftest import (
    count_rows,
    drop_table,
    poll_until,
    post_batch,
    post_events,
    post_ndjson,
    require,
)
from tests.e2e.flows import shapes
from tests.e2e.flows.conftest import EngineAPI

# Argo polls the deploy repo every 300 s with up to 60 s of jitter, and the pod
# then rolls on the new ConfigMap checksum, so a routing change is minutes away.
ROUTING_DEADLINE = 900.0
# Once the routing is live the record's own trip is seconds; this only covers a
# loader mid-roll.
LANDING_DEADLINE = 240.0
# A fetcher polls its upstream on its own schedule, and the first fetch follows
# the pod becoming ready.
FETCH_DEADLINE = 600.0

# Events in one batched POST, enough that a receiver forwarding the body whole
# lands a count nothing could mistake for the batch.
BATCH_SIZE = 25

# A shape waits ROUTING then LANDING once per expectation, so the widest one bounds
# the run.
_WIDEST_SHAPE = max(len(shape.expect) for shape in shapes.load_shapes())
# pyproject's 300 s hang guard is shorter than one Argo poll and its thread method
# kills the process rather than failing the case, so this suite carries its own,
# above the deadlines that are the real guard here.
SUITE_TIMEOUT = _WIDEST_SHAPE * (ROUTING_DEADLINE + LANDING_DEADLINE) + FETCH_DEADLINE + 300.0

pytestmark = [pytest.mark.live, pytest.mark.timeout(SUITE_TIMEOUT)]


def _marker() -> str:
    return f"flow{uuid.uuid4().hex}"


def _bodies(shape: shapes.FlowShape, marker: str) -> list[dict[str, Any]]:
    """The shape's payload with this run's marker substituted in.

    Replaced rather than formatted: the records are JSON and str.format would
    read every brace in them as a field.
    """
    import json

    return [json.loads(json.dumps(record).replace("{marker}", marker)) for record in shape.payload]


def _delete(engine: EngineAPI, names: tuple[str, ...]) -> None:
    """Remove each source, tolerating one that is not there."""
    for name in names:
        response = engine.call("DELETE", f"/sources/{name}")
        assert response.status_code in (200, 204, 404), (
            f"could not remove {name}: {response.status_code} {response.text}"
        )


def _catchall(engine: EngineAPI) -> tuple[str, str]:
    """The catch-all's two names on this deployment: (source label, table).

    Two reads because they are two facts, each owned by a different app: the
    receiver stamps the label, the loader picks the table. Read rather than
    assumed - the name is being changed, so a suite that hardcoded either one
    would assert against a table the deployment does not have the moment it
    moved, and reading only one hides a rename that landed on one side.
    """
    receiver = engine.json("GET", f"/apps/dfe-receiver/{_receiver_instance(engine)}/routing")
    loader = engine.json("GET", f"/apps/dfe-loader/{_instance(engine, 'dfe-loader')}/routing")
    label = ((receiver.get("deployed") or {}).get("routing") or {}).get("default_source")
    table = ((loader.get("deployed") or {}).get("routing") or {}).get("default_table")
    return str(label or shapes.CATCHALL), str(table or shapes.CATCHALL)


def _instance(engine: EngineAPI, service: str) -> str:
    """The stack's single instance of a stack-scoped app."""
    entry = next(a for a in engine.json("GET", "/apps") if a["service"] == service)
    assert entry["instances"], f"the deployment carries no {service} instance"
    return str(entry["instances"][0])


def _receiver_instance(engine: EngineAPI) -> str:
    """The stack's single receiver instance, as the catalogue names it."""
    return _instance(engine, "dfe-receiver")


def _instances(engine: EngineAPI, service: str) -> list[str]:
    apps = engine.json("GET", "/apps")
    entry = next((a for a in apps if a["service"] == service), None)
    return [str(i) for i in (entry or {}).get("instances", [])]


def _create_and_deploy(
    engine: EngineAPI, shape: shapes.FlowShape, transport: str
) -> list[dict[str, Any]]:
    """Create every source of the shape on *transport*, then deploy each.

    Returns one deploy result per source, in creation order.
    """
    deploys: list[dict[str, Any]] = []
    for body in shape.sources:
        created = engine.call("POST", "/sources", {**body, "transport": transport})
        assert created.status_code == 201, (
            f"the deployment refused {body['source']!r} on {transport}: "
            f"{created.status_code} {created.text}"
        )
        name = str(body["source"])
        deployed = engine.json("POST", f"/sources/{name}/deploy")
        assert deployed["applied"], f"{name}: deploy did not apply: {deployed}"
        assert not deployed["apps_sync_error"], (
            f"{name}: the apps could not follow the source: {deployed['apps_sync_error']}"
        )
        deploys.append(deployed)
    return deploys


def _upload_transform(engine: EngineAPI, shape: shapes.FlowShape) -> None:
    """Put the shape's program into each transform instance the deploy created."""
    if shape.transform_file is None:
        return
    filename, content = shape.transform_file
    for body in shape.sources:
        transform = body.get("transform")
        if not transform:
            continue
        service = f"dfe-transform-{transform['engine']}"
        engine.json(
            "PUT",
            f"/apps/{service}/{body['source']}/files/transforms/{filename}",
            {"content": content},
        )


def _assert_topics(deployed: dict[str, Any], expectation: shapes.FlowExpectation) -> None:
    """On the bus, the deploy reports exactly the topics this source needs.

    The transformed topic is set exactly when the source has a transform, and a
    ``_load`` topic left behind for a source without one starves it: the loader
    suppresses ``_land`` whenever the sibling exists.
    """
    ensured = set(deployed.get("topics_ensured") or ())
    assert not deployed.get("topics_failed"), (
        f"{expectation.source}: topics the broker refused: {deployed['topics_failed']}"
    )
    if expectation.topic:
        assert expectation.topic in ensured, (
            f"{expectation.source}: landing topic {expectation.topic} not among {sorted(ensured)}"
        )
    load = f"{expectation.source}_load"
    if expectation.load_topic:
        assert expectation.load_topic in ensured, (
            f"{expectation.source}: a transformed source needs {expectation.load_topic}, "
            f"got {sorted(ensured)}"
        )
    else:
        assert load not in ensured, (
            f"{expectation.source}: no transform, so {load} must not exist - the loader "
            "suppresses the landing topic whenever it does"
        )


def _assert_receiver_routing(
    engine: EngineAPI,
    instance: str,
    shape: shapes.FlowShape,
    transport: str,
    expectation: shapes.FlowExpectation,
) -> None:
    """The receiver's OVERLAY names this source, and on direct its destination.

    Read from ``deployed`` rather than ``compiled``: compiled is what the sources
    call for, so asserting on it would only prove the compiler ran. What has to
    be true is that the deploy repo now carries it, which is what Argo applies -
    hence the drift check beside it.
    """
    routing = engine.json("GET", f"/apps/dfe-receiver/{instance}/routing")
    assert not routing["drift"], (
        f"the receiver overlay disagrees with the sources after deploying "
        f"{expectation.source}, so what Argo applies is not what was asked for"
    )
    compiled = routing["deployed"]
    rules = compiled.get("routing", {}).get("source_rules") or []
    assert any(r["source"] == expectation.source for r in rules), (
        f"{expectation.source}: no receiver rule names it; compiled rules are "
        f"{[r['source'] for r in rules]}"
    )
    if transport != "direct" or expectation.destination is None:
        return
    declared = next(s for s in shape.sources if s["source"] == expectation.source)
    match = declared["match"]
    destinations = compiled.get("destinations", {})
    named = [
        r["destination"]
        for r in destinations.get("rules") or []
        if r["match_field"] == match["field"] and r["match_value"] == match["value"]
    ]
    assert named == [expectation.destination], (
        f"{expectation.source}: on direct the receiver must send {match['field']}="
        f"{match['value']} to {expectation.destination}, and it names {named}"
    )


def _wait_until_routed(e2e, ch_client, shape: shapes.FlowShape, expectation) -> None:
    """Probe until a record actually reaches the source table.

    The probe rows are the WAIT: before the receiver has rolled onto the new
    routing they land in default, which is why the payload the case asserts on is
    only sent once this returns.

    One marker for every probe, so a row posted on an earlier pass satisfies a
    later count. A marker per pass would only ever see its own row if the whole
    trip finished between the post and the query, which it does not have to.
    """
    table = f"{e2e.ch_db}.{expectation.table}"
    probe = _marker()

    def routed() -> int:
        post_events(e2e, _bodies(shape, probe))
        return count_rows(ch_client, table, marker=probe)

    poll_until(
        routed,
        timeout=ROUTING_DEADLINE,
        interval=20.0,
        desc=f"the receiver to route {expectation.source} into {table}",
    )


@pytest.fixture(scope="session")
def catchall(engine: EngineAPI) -> tuple[str, str]:
    """The catch-all's (source label, table) on this deployment, read once."""
    return _catchall(engine)


@pytest.fixture
def flow_sources(engine: EngineAPI, e2e, ch_client):
    """Every source a case creates, removed when it ends however it ended.

    The table goes with it. Deleting a source deliberately leaves its table in
    place -- the records outlive the definition -- so a suite that only deleted
    sources would leave one table per shape per run behind on the deployment.
    """
    created: list[str] = []
    yield created
    _delete(engine, tuple(created))
    for name in created:
        try:
            drop_table(ch_client, e2e.ch_db, name)
        except Exception as exc:  # a table this run never got as far as creating
            print(f"could not drop {e2e.ch_db}.{name}: {exc}")


class TestFlows:
    """One shape, one transport, end to end."""

    def test_the_shape_runs_end_to_end(
        self,
        shape: shapes.FlowShape,
        transport: str,
        engine,
        e2e,
        ch_client,
        flow_sources,
        catchall: tuple[str, str],
        carried: tuple[str, ...],
        offered: tuple[str, ...],
    ) -> None:
        require(e2e, "receiver_url", "ch_host")
        refusal = shape.refusal(transport, carried, offered)
        if refusal:
            pytest.skip(
                f"{shapes.EXPECTED_SKIP} this deployment refuses {shape.name} on "
                f"{transport} ({refusal}), so there is no landing to prove - the "
                "refusal case asserts it instead"
            )
        reason = shape.skip_reason(transport)
        if reason:
            pytest.skip(reason)
        if shape.origin == "catalogue":
            self._catalogue_flow(shape, transport, engine, e2e, ch_client, flow_sources)
            return
        if shape.origin == "default":
            self._default_flow(shape, e2e, ch_client, catchall)
            return

        _delete(engine, shape.names())
        flow_sources.extend(shape.names())
        deploys = _create_and_deploy(engine, shape, transport)
        _upload_transform(engine, shape)
        receiver_instance = _receiver_instance(engine)

        for deployed, expectation in zip(deploys, shape.expect, strict=True):
            if transport == "bus":
                _assert_topics(deployed, expectation)
            if shape.origin == "receiver":
                _assert_receiver_routing(engine, receiver_instance, shape, transport, expectation)
            else:
                assert expectation.source in _instances(engine, "dfe-fetcher"), (
                    f"{expectation.source}: the deploy reported "
                    f"{deployed['apps_synced']} but no fetcher instance carries the name"
                )

        if shape.origin == "fetcher":
            self._assert_fetched(shape, e2e, ch_client)
        else:
            self._assert_posted(shape, e2e, ch_client, catchall[1])

        # Deleting HERE is the assertion that a source can be removed; the
        # fixture repeats it and drops the table, so the names stay on its list.
        _delete(engine, shape.names())
        if shape.origin == "fetcher":
            poll_until(
                lambda: all(n not in _instances(engine, "dfe-fetcher") for n in shape.names()),
                timeout=120.0,
                desc="the fetcher instances to go with their sources",
            )

    def _assert_posted(self, shape: shapes.FlowShape, e2e, ch_client, catchall_table: str) -> None:
        """Send the payload once the routing is live, and check both halves."""
        for expectation in shape.expect:
            _wait_until_routed(e2e, ch_client, shape, expectation)

        marker = _marker()
        post_events(e2e, _bodies(shape, marker))
        for expectation in shape.expect:
            table = f"{e2e.ch_db}.{expectation.table}"
            landed = poll_until(
                lambda t=table: count_rows(ch_client, t, marker=marker),
                timeout=LANDING_DEADLINE,
                desc=f"the marked records in {table}",
            )
            assert landed > 0
            if expectation.marker:
                assert (
                    count_rows(ch_client, table, marker=marker, contains=expectation.marker)
                    == landed
                ), (
                    f"{expectation.source}: {expectation.marker} is set only by the "
                    "transform, so a row without it reached the table untransformed"
                )
        strays = count_rows(ch_client, f"{e2e.ch_db}.{catchall_table}", marker=marker)
        assert strays == 0, (
            f"{strays} record(s) of this payload took the catch-all flow into "
            f"{catchall_table}, so the routing did not hold for every record the shape sent"
        )
        for other in shape.expect:
            for expectation in shape.expect:
                if other.table == expectation.table:
                    continue
                seen = count_rows(
                    ch_client,
                    f"{e2e.ch_db}.{other.table}",
                    where=f"_source = '{expectation.source}'",
                    marker=marker,
                )
                assert seen == 0, (
                    f"{expectation.source} records reached {other.table}, so the two rules "
                    "do not separate their sources"
                )

    def _assert_fetched(self, shape: shapes.FlowShape, e2e, ch_client) -> None:
        """A fetcher pulls on its own schedule, so the wait is for its first fetch."""
        for expectation in shape.expect:
            table = f"{e2e.ch_db}.{expectation.table}"
            where = (
                f"_source = '{expectation.source}' AND _timestamp_load > now() - INTERVAL 30 MINUTE"
            )
            landed = poll_until(
                lambda t=table, w=where, m=expectation.marker: count_rows(
                    ch_client, t, where=w, contains=m
                ),
                timeout=FETCH_DEADLINE,
                interval=20.0,
                desc=f"the fetcher's first records in {table}",
            )
            assert landed > 0

    def _default_flow(
        self, shape: shapes.FlowShape, e2e, ch_client, catchall: tuple[str, str]
    ) -> None:
        """An unmatched record lands in the catch-all table, under one name.

        The receiver names the label and the loader names the table, so the two
        are asserted to AGREE before the landing is: a deployment where they
        differ puts records where nobody looking under the source name will find
        them, and the landing check alone would pass right through it.
        """
        label, table_name = catchall
        assert label == table_name, (
            f"the receiver stamps an unmatched record {label!r} and the loader lands it in "
            f"{table_name!r}, so nothing that reads the catch-all by name finds its records"
        )
        expectation = shape.expect[0].substitute(catchall=table_name)
        marker = _marker()
        post_events(e2e, _bodies(shape, marker))
        table = f"{e2e.ch_db}.{expectation.table}"
        landed = poll_until(
            lambda: count_rows(ch_client, table, marker=marker),
            timeout=LANDING_DEADLINE,
            desc=f"the unmatched record in {table}",
        )
        assert landed > 0

    def _catalogue_flow(
        self, shape: shapes.FlowShape, transport: str, engine, e2e, ch_client, flow_sources
    ) -> None:
        """A source compiled from a mounted catalogue entry, where one is mounted."""
        offered = engine.json("GET", "/sources/catalogue")
        entries = offered.get("entries") if isinstance(offered, dict) else offered
        if not entries:
            pytest.skip(
                f"{shapes.EXPECTED_SKIP} this deployment mounts nothing at "
                "/etc/dfe/catalogue/sources.yaml (or DFE_SOURCE_CATALOGUE_FILE), so the "
                "engine offers no entry to compile; the file is a dfe-transform-elastic "
                "release asset (dfe-transform-elastic#18)"
            )
        entry = str(entries[0]["name"] if isinstance(entries[0], dict) else entries[0])
        created = engine.call("POST", f"/sources/from-catalogue/{entry}", {"transport": transport})
        assert created.status_code in (200, 201), (
            f"the catalogue entry {entry!r} would not compile into a source: "
            f"{created.status_code} {created.text}"
        )
        name = str(created.json()["source"])
        flow_sources.append(name)
        deployed = engine.json("POST", f"/sources/{name}/deploy")
        assert deployed["applied"], f"{name}: deploy did not apply: {deployed}"
        expectation = shape.expect[0].substitute(entry=name)
        if transport == "bus":
            _assert_topics(deployed, expectation)


def test_a_flow_the_deployment_cannot_run_is_refused(
    shape: shapes.FlowShape,
    transport: str,
    engine,
    carried: tuple[str, ...],
    offered: tuple[str, ...],
) -> None:
    """The refusal is the assertion: a flow that cannot run must fail at save.

    Not the same claim as the skip beside it. The skip says this suite cannot
    prove the landing half here; this says the deployment says so too, and says
    why, rather than accepting the source and never starting the pod.

    On a bus deployment asked to prove both transports, this is what the direct
    half of the run proves. On a tier that deploys no fetcher, it is what the
    fetcher shapes prove.
    """
    declared = shape.refusal(transport, carried, offered)
    if declared is None:
        pytest.skip(
            f"{shapes.EXPECTED_SKIP} this deployment carries {transport}, deploys every "
            f"app the {shape.name} flow needs, and each of them carries the transport, "
            "so the end-to-end case proves it"
        )
    if not shape.sources:
        pytest.skip(
            f"{shapes.EXPECTED_SKIP} the {shape.name} shape writes no source of its own, "
            "so it has nothing to be refused at save"
        )
    for body in shape.sources:
        response = engine.call("POST", "/sources", {**body, "transport": transport})
        assert response.status_code >= 400, (
            f"{body['source']!r} was accepted on {transport}, which cannot run it"
        )
        assert declared in response.text, (
            f"{body['source']!r} was refused on {transport}, but not for the "
            f"declared reason ({declared!r}): {response.text}"
        )


def _element(marker: str, index: int) -> str:
    """This run's token for one element of a batch.

    Fixed width so no element's token is a substring of another's, which is what
    lets a row be counted by the element it came from.
    """
    return f"{marker}-{index:04d}"


def _batch(marker: str) -> list[dict[str, Any]]:
    """One distinct record per element, each carrying its own index token.

    The index rides inside a string rather than as a JSON number because the row
    is matched on a ``_raw`` substring, and how a number is rendered belongs to
    whatever serialised it.
    """
    return [
        {"message": f"flow e2e {_element(marker, index)} batched", "host": {"name": "flow-e2e"}}
        for index in range(BATCH_SIZE)
    ]


def _assert_one_row_per_element(e2e, ch_client, catchall_table: str, marker: str) -> None:
    """Every element of the batch landed, once each.

    Both halves are the assertion. The count catches a receiver that forwards the
    batch whole, which answers 202 and lands nothing at all; the per-element
    count catches a split that duplicated or dropped one.
    """
    table = f"{e2e.ch_db}.{catchall_table}"
    poll_until(
        lambda: count_rows(ch_client, table, marker=marker) >= BATCH_SIZE,
        timeout=LANDING_DEADLINE,
        desc=f"all {BATCH_SIZE} records of the batch in {table}",
    )
    landed = count_rows(ch_client, table, marker=marker)
    assert landed == BATCH_SIZE, f"a batch of {BATCH_SIZE} events landed {landed} row(s) in {table}"
    for index in range(BATCH_SIZE):
        seen = count_rows(ch_client, table, contains=_element(marker, index))
        assert seen == 1, f"element {index} of the batch landed {seen} row(s) in {table}"


class TestABatchedPost:
    """One POST carrying many events lands one row per event.

    Both bodies take the catch-all flow rather than a source of their own: what
    is under test is the receiver splitting the body, the catch-all is deployed
    on every profile and either transport, and going through it means no Argo
    poll stands between the post and the assertion.
    """

    def test_a_json_array_lands_one_row_per_element(
        self, e2e, ch_client, catchall: tuple[str, str]
    ) -> None:
        require(e2e, "receiver_url", "ch_host")
        marker = _marker()
        post_batch(e2e, _batch(marker))
        _assert_one_row_per_element(e2e, ch_client, catchall[1], marker)

    def test_an_ndjson_body_lands_one_row_per_line(
        self, e2e, ch_client, catchall: tuple[str, str]
    ) -> None:
        require(e2e, "receiver_url", "ch_host")
        marker = _marker()
        post_ndjson(e2e, _batch(marker))
        _assert_one_row_per_element(e2e, ch_client, catchall[1], marker)
