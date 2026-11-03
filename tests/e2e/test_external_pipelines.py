#  Project:      dfe-engine
#  File:         tests/e2e/test_external_pipelines.py
#  Purpose:      A real Vector and a real Fluent Bit as a source's origin and transform, on a live stack
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The customer's own pipeline as a source's origin and as its transform, on a live stack.

Vector is the origin: it produces three records straight to the source's landing
topic. Fluent Bit is the transform: the receiver lands three records, and Fluent Bit
reads them in the source's external consumer group and writes the load topic. Every
topic and group comes from the source's ``connection`` block, the way a customer would
configure their pipeline.

Each shape sends a record carrying every common header field, one carrying none, and
one claiming another tenant's ``_org_id``. Each must land once in the source's table,
in the source's own org with the source's own ``_source``.

Live only. The pipelines run on the docker daemon the environment points at, and reach
the broker at ``DFE_E2E_KAFKA_BOOTSTRAP`` on the network ``DFE_E2E_DOCKER_NETWORK``
names, which for a dfe-docker stack is its compose network. Config is copied into each
container, so a remote daemon works the same as a local one.
"""

import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    PAYLOAD_TEXT,
    E2EConfig,
    count_rows,
    drop_table,
    must,
    poll_until,
    post_events,
    require,
)
from tests.e2e.engine_api import EngineAPI

pytestmark = pytest.mark.live

# The images dfe-receiver's peer tests pin (tests/common/peers.rs), so the suite runs one version of each.
VECTOR_IMAGE = "timberio/vector:0.60.0-distroless-libc@sha256:a9ed9a4710bdc2efa66e94c1e8edc7b439709e6e50079f1e1b2b1bf479d387da"
FLUENT_BIT_IMAGE = "fluent/fluent-bit:5.1.5@sha256:2740f387f6e68fab66d28e4fa6a853387b9cab6ffb4cb78958f69bba4eb0e7b4"

DEFAULT_ORG_ID = "default"
FOREIGN_ORG_ID = "not-this-sources-tenant"
KINDS = ("compliant", "bare", "foreign")
LANDING_WAIT = 240.0
CONFIG_DIR = "/etc/dfe-e2e"


def records(name: str, marker: str) -> list[dict]:
    """A compliant record, a bare one, and one claiming another tenant."""
    return [
        {
            "_timestamp": "2026-11-03T04:05:06.789Z",
            "_org_id": DEFAULT_ORG_ID,
            "_source": name,
            "_tags": {"pipeline": "e2e"},
            "_timestamp_received": "2026-11-03T04:05:06.123Z",
            "message": f"{marker}-compliant",
        },
        {"message": f"{marker}-bare"},
        {"_org_id": FOREIGN_ORG_ID, "message": f"{marker}-foreign"},
    ]


def vector_config(connection: dict, bootstrap: str) -> dict:
    """Vector reading a file of records and producing them to the source's landing topic.

    The remap replaces each event with its parsed line, which drops the fields the file
    source adds, so the bare record reaches DFE bare.
    """
    return {
        "data_dir": "/tmp",
        "sources": {
            "records": {
                "type": "file",
                "include": [f"{CONFIG_DIR}/records.ndjson"],
                "read_from": "beginning",
            }
        },
        "transforms": {
            "bare": {
                "type": "remap",
                "inputs": ["records"],
                "source": ". = parse_json!(string!(.message))",
            }
        },
        "sinks": {
            "dfe": {
                "type": "kafka",
                "inputs": ["bare"],
                "bootstrap_servers": bootstrap,
                "topic": connection["writes"],
                "encoding": {"codec": "json"},
            }
        },
    }


def fluent_bit_config(connection: dict, bootstrap: str) -> dict:
    """Fluent Bit consuming the landing topic in the external group and producing the load topic.

    in_kafka nests each message under ``payload`` beside its Kafka metadata, so the nest
    filter lifts it back to the top level and the metadata and routing key are dropped.
    The output's timestamp goes in its own key so the bare record keeps no timestamp.
    """
    return {
        "pipeline": {
            "inputs": [
                {
                    "name": "kafka",
                    "brokers": bootstrap,
                    "topics": connection["reads"],
                    "group_id": connection["consumer_group"],
                    "rdkafka.auto.offset.reset": "earliest",
                    "format": "json",
                }
            ],
            "filters": [
                {"name": "nest", "match": "*", "operation": "lift", "nested_under": "payload"},
                {
                    "name": "modify",
                    "match": "*",
                    "remove": ["topic", "partition", "offset", "key", "error", "e2e_route"],
                },
            ],
            "outputs": [
                {
                    "name": "kafka",
                    "match": "*",
                    "brokers": bootstrap,
                    "topics": connection["writes"],
                    "format": "json",
                    "timestamp_key": "fluent_bit_time",
                }
            ],
        }
    }


def _kafka() -> tuple[str, str | None]:
    """The broker address a container reaches, and the docker network it reaches it on."""
    bootstrap = os.getenv("DFE_E2E_KAFKA_BOOTSTRAP")
    if not bootstrap:
        pytest.skip("live e2e not configured: missing DFE_E2E_KAFKA_BOOTSTRAP")
    return bootstrap, os.getenv("DFE_E2E_DOCKER_NETWORK") or None


@pytest.fixture(scope="module")
def engine(e2e: E2EConfig) -> EngineAPI:
    """The engine API, or the skip that says which var is missing."""
    require(e2e, "engine_url", "engine_password", "ch_host")
    api = EngineAPI(
        base=must(e2e.engine_url).rstrip("/"),
        user=e2e.engine_user,
        password=must(e2e.engine_password),
        verify=e2e.verify,
        new_password=e2e.engine_new_password,
    )
    api.login()
    return api


@pytest.fixture
def cleanup(engine: EngineAPI, e2e: E2EConfig, ch_client) -> Iterator[list[str]]:
    """Every source a test made, deleted with its table and topics when it ends."""
    names: list[str] = []
    yield names
    for name in names:
        engine.call("DELETE", f"/api/v1/sources/{name}", None)
        drop_table(ch_client, e2e.ch_db, name)


def _deployed(engine: EngineAPI, name: str, **shape) -> dict:
    """Create and deploy a bus source, and return the connection block it publishes."""
    body = {
        "source": name,
        "enabled": True,
        "transport": "bus",
        "header": {"type": "timeseries", "version": "1.0.3"},
        "schema_config": {"engine": "MergeTree"},
        **shape,
    }
    created = engine.call("POST", "/api/v1/sources", body)
    assert created.status_code == 201, created.text
    deployed = engine.call("POST", f"/api/v1/sources/{name}/deploy", None)
    assert deployed.status_code == 200, deployed.text
    connection = engine.json("GET", f"/api/v1/sources/{name}")["connection"]
    assert connection is not None, "an external source publishes its connection block"
    return connection


def _start(image: str, network: str | None, files: dict[str, str], args: list[str]) -> str:
    """Create a pipeline container, copy its files in, and start it."""
    container = f"dfe-engine-test-e2e-external-{uuid.uuid4().hex[:8]}"
    create = ["docker", "create", "--name", container, "--label", "io.hyperi.test=dfe-engine-e2e"]
    if network:
        create += ["--network", network]
    subprocess.run([*create, image, *args], check=True, capture_output=True)
    with tempfile.TemporaryDirectory(prefix="dfe-e2e-pipeline-") as work:
        for filename, text in files.items():
            (Path(work) / filename).write_text(text, encoding="utf-8")
        subprocess.run(
            ["docker", "cp", f"{work}/.", f"{container}:{CONFIG_DIR}"],
            check=True,
            capture_output=True,
        )
    subprocess.run(["docker", "start", container], check=True, capture_output=True)
    return container


def _remove(container: str) -> None:
    subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)


def _assert_landed(ch_client, e2e: E2EConfig, table: str, marker: str) -> None:
    """Each record once, all three in the source's own org under its own name."""
    qualified = f"{e2e.ch_db}.`{table}`"
    for kind in KINDS:
        poll_until(
            lambda kind=kind: count_rows(ch_client, qualified, marker=f"{marker}-{kind}"),
            timeout=LANDING_WAIT,
            interval=5,
            desc=f"the {kind} record in {qualified}",
        )
        assert count_rows(ch_client, qualified, marker=f"{marker}-{kind}") == 1, kind

    rows = ch_client.query(
        f"SELECT {PAYLOAD_TEXT}, _org_id, _source FROM {qualified} WHERE {PAYLOAD_TEXT} LIKE %(m)s",
        parameters={"m": f"%{marker}%"},
    ).result_rows
    for kind in KINDS:
        landed = [row for row in rows if f"{marker}-{kind}" in row[0]]
        _, org_id, source = landed[0]
        assert org_id == DEFAULT_ORG_ID, f"{kind} landed in org {org_id!r}"
        assert source == table, f"{kind} carries _source {source!r}"


def test_vector_as_the_origin(engine, e2e, ch_client, cleanup) -> None:
    bootstrap, network = _kafka()
    name = f"ext-vec-{uuid.uuid4().hex[:6]}"
    marker = f"e2e-{uuid.uuid4().hex}"
    connection = _deployed(engine, name, external={"credential_ref": "env:E2E_UNUSED"})
    cleanup.append(name)
    assert connection["role"] == "origin"

    lines = "".join(json.dumps(record) + "\n" for record in records(name, marker))
    container = _start(
        VECTOR_IMAGE,
        network,
        {
            "vector.json": json.dumps(vector_config(connection, bootstrap)),
            "records.ndjson": lines,
        },
        ["--config", f"{CONFIG_DIR}/vector.json"],
    )
    try:
        _assert_landed(ch_client, e2e, name, marker)
    finally:
        _remove(container)


def test_fluent_bit_as_the_transform(engine, e2e, ch_client, cleanup) -> None:
    require(e2e, "receiver_url")
    bootstrap, network = _kafka()
    name = f"ext-fb-{uuid.uuid4().hex[:6]}"
    marker = f"e2e-{uuid.uuid4().hex}"
    connection = _deployed(
        engine,
        name,
        match={"field": "e2e_route", "operator": "equals", "value": name},
        transform={"engine": "external", "credential_ref": "env:E2E_UNUSED"},
    )
    cleanup.append(name)
    assert connection["role"] == "transform"

    container = _start(
        FLUENT_BIT_IMAGE,
        network,
        {"fluent-bit.yaml": json.dumps(fluent_bit_config(connection, bootstrap))},
        ["-c", f"{CONFIG_DIR}/fluent-bit.yaml"],
    )
    try:
        post_events(e2e, [{**record, "e2e_route": name} for record in records(name, marker)])
        _assert_landed(ch_client, e2e, name, marker)
        lag = poll_until(
            lambda: engine.json("GET", f"/api/v1/sources/{name}/lag"),
            done=lambda reading: reading["lag"] == 0,
            timeout=60,
            desc=f"{connection['consumer_group']} to commit past every record",
        )
        assert lag["consumer_group"] == connection["consumer_group"]
        assert lag["committed"] is True
    finally:
        _remove(container)
