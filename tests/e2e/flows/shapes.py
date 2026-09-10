#  Project:      dfe-engine
#  File:         tests/e2e/flows/shapes.py
#  Purpose:      Read the flow fixtures, and decide which transport runs which shape
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One fixture set, read once, for every lane that runs the flow suite.

A shape is a directory under ``fixtures/``: the source definitions to create, the
payload to post, and what must be true afterwards. The suite is parametrised over
shapes x transports, so a new flow shape is a directory rather than a test.

Nothing here talks to a deployment. It parses, validates and answers "can this
shape run on this transport", which is what the unit tests exercise and what
``test_flows.py`` then acts on.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

FIXTURES = Path(__file__).parent / "fixtures"

# The deployment names its transports after what carries them; a source names
# only which of the two it is on. dfe-ops derives the left word from the profile
# and dfe-docker's service_profiles.yaml declares it, so this is the one place
# the two vocabularies meet.
DEPLOYMENT_TRANSPORTS: dict[str, str] = {"kafka": "bus", "grpc": "direct"}

# Every skip this suite is allowed to report carries this prefix; anything else
# fails the run (see conftest.py).
EXPECTED_SKIP = "EXPECTED-SKIP:"

# What an unmatched record is stamped with, and the table it lands in. Fixtures
# write {catchall} and the run fills it from the receiver's compiled
# routing.default_source, because the name belongs to the deployed release.
CATCHALL = "main"

_ORIGINS = ("receiver", "fetcher", "default", "catalogue")


class FixtureError(ValueError):
    """A fixture directory does not describe a runnable flow."""


@dataclass(frozen=True, slots=True)
class FlowExpectation:
    """What one source of a shape must produce."""

    source: str
    table: str
    """ClickHouse table the records must land in."""

    topic: str | None
    """Bus only: the landing topic the deploy must report as ensured."""

    load_topic: str | None
    """Bus only: the transformed topic, set exactly when the source has a transform."""

    marker: str | None
    """A column only the transform sets, so a pass-through row cannot satisfy the check."""

    destination: str | None
    """Direct only: the receiver destination the compiled routing must name."""

    def substitute(self, **values: str) -> FlowExpectation:
        """The same expectation with its placeholders filled in.

        Two names are the deployment's to give, not the fixture's: ``{entry}``,
        because a catalogue shape does not know its source until the deployment
        says what it offers, and ``{catchall}``, because the source an unmatched
        record is stamped with is whatever the deployed release calls it.
        """

        def fill(value: str | None) -> str | None:
            if not value:
                return value
            for name, replacement in values.items():
                value = value.replace("{" + name + "}", replacement)
            return value

        return FlowExpectation(
            source=str(fill(self.source)),
            table=str(fill(self.table)),
            topic=fill(self.topic),
            load_topic=fill(self.load_topic),
            marker=fill(self.marker),
            destination=fill(self.destination),
        )


@dataclass(frozen=True, slots=True)
class FlowShape:
    """One flow shape: what to create, what to send, and what must follow."""

    name: str
    description: str
    origin: str
    sources: tuple[dict[str, Any], ...]
    payload: tuple[dict[str, Any], ...]
    expect: tuple[FlowExpectation, ...]
    expected_skip: Mapping[str, str]
    """transport -> why this shape cannot run there yet."""

    refused: Mapping[str, str]
    """transport -> the substring the API's refusal must carry, where an app in this
    shape's flow does not carry that transport. What the DEPLOYMENT carries is read
    off the deployment instead, so no fixture repeats it."""

    transform_file: tuple[str, str] | None
    """(filename, content) uploaded into the transform instance's file set."""

    catalogue_intake: str | None
    """Catalogue shapes only: the intake kind an offered entry must support."""

    def names(self) -> tuple[str, ...]:
        """Every source name this shape creates, in creation order."""
        return tuple(str(s["source"]) for s in self.sources)

    def skip_reason(self, transport: str) -> str | None:
        """Why this shape does not run on *transport*, or None when it does.

        A shape the deployment REFUSES still runs: the refusal is the assertion.
        """
        reason = self.expected_skip.get(transport)
        return f"{EXPECTED_SKIP} {reason}" if reason else None

    def refusal(self, transport: str, carried: Sequence[str]) -> str | None:
        """The refusal the API must give on *transport* here, or None when it must save.

        Two refusals reach the same save. A deployment binds its stages to ONE
        transport, so a source on the other is refused whatever the shape is -
        that is read off the deployment at run time, never declared per fixture.
        What an app in this shape's flow cannot carry is the fixture's own.

        Args:
            transport: The source transport the case is running.
            carried: What the deployment reports it carries.

        Returns:
            The substring the refusal must contain, or None.
        """
        if transport not in carried:
            return f"asks for the {transport} transport"
        return self.refused.get(transport)


def _expectation(raw: Any, *, shape: str) -> FlowExpectation:
    if not isinstance(raw, dict) or "source" not in raw or "table" not in raw:
        raise FixtureError(f"{shape}: each expect entry needs at least source and table")
    return FlowExpectation(
        source=str(raw["source"]),
        table=str(raw["table"]),
        topic=raw.get("topic"),
        load_topic=raw.get("load_topic"),
        marker=raw.get("marker"),
        destination=raw.get("destination"),
    )


def _payload(path: Path, *, shape: str) -> tuple[dict[str, Any], ...]:
    """The NDJSON body, one object per line. Absent for a shape that posts nothing."""
    if not path.is_file():
        return ()
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FixtureError(f"{shape}: payload.ndjson line {number} is not JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise FixtureError(f"{shape}: payload.ndjson line {number} is not an object")
        records.append(record)
    return tuple(records)


def _transport_map(raw: Any, *, shape: str, key: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise FixtureError(f"{shape}: {key} must map a transport to a reason")
    unknown = set(raw) - set(DEPLOYMENT_TRANSPORTS.values())
    if unknown:
        raise FixtureError(
            f"{shape}: {key} names unknown transport(s) {', '.join(sorted(unknown))}"
        )
    return {str(k): str(v) for k, v in raw.items()}


def load_shape(directory: Path) -> FlowShape:
    """Parse one fixture directory, or say what is wrong with it.

    Args:
        directory: A directory under ``fixtures/``.

    Returns:
        The parsed shape.

    Raises:
        FixtureError: The directory is missing a file or declares an impossible flow.
    """
    shape = directory.name
    source_file = directory / "source.yaml"
    expect_file = directory / "expect.yaml"
    for required in (source_file, expect_file):
        if not required.is_file():
            raise FixtureError(f"{shape}: {required.name} is missing")

    declared = yaml.safe_load(source_file.read_text(encoding="utf-8")) or {}
    expected = yaml.safe_load(expect_file.read_text(encoding="utf-8")) or {}
    origin = str(expected.get("origin", ""))
    if origin not in _ORIGINS:
        raise FixtureError(f"{shape}: origin must be one of {', '.join(_ORIGINS)}, got {origin!r}")

    sources = tuple(declared.get("sources") or ())
    for entry in sources:
        if not isinstance(entry, dict) or not entry.get("source"):
            raise FixtureError(f"{shape}: every entry under sources needs a source name")
    if origin in ("receiver", "fetcher") and not sources:
        raise FixtureError(f"{shape}: a {origin}-origin shape must declare at least one source")

    payload = _payload(directory / "payload.ndjson", shape=shape)
    # A fetcher pulls its own records and a catalogue entry is created from the
    # catalogue, so only the shapes that post through the receiver carry a body.
    if origin in ("receiver", "default") and not payload:
        raise FixtureError(f"{shape}: a {origin}-origin shape must carry payload.ndjson")
    if origin == "fetcher" and payload:
        raise FixtureError(
            f"{shape}: a fetcher-origin shape posts nothing, so it carries no payload"
        )

    expectations = tuple(_expectation(e, shape=shape) for e in expected.get("expect") or ())
    if not expectations:
        raise FixtureError(f"{shape}: expect.yaml declares nothing to assert")
    known = set(str(s["source"]) for s in sources)
    if origin in ("receiver", "fetcher"):
        for expectation in expectations:
            if expectation.source not in known:
                raise FixtureError(
                    f"{shape}: expect names {expectation.source!r}, which source.yaml does not create"
                )

    transform_file: tuple[str, str] | None = None
    declared_file = expected.get("transform_file")
    if declared_file:
        program = directory / str(declared_file)
        if not program.is_file():
            raise FixtureError(f"{shape}: transform_file names {declared_file}, which is not there")
        transform_file = (program.name, program.read_text(encoding="utf-8"))
    wants_transform = any("transform" in s for s in sources)
    if wants_transform and transform_file is None:
        raise FixtureError(
            f"{shape}: a source declares a transform, so the shape must ship the program it runs"
        )

    catalogue_intake = (expected.get("catalogue") or {}).get("intake")
    if origin == "catalogue" and not catalogue_intake:
        raise FixtureError(f"{shape}: a catalogue shape must say which intake an entry needs")

    return FlowShape(
        name=shape,
        description=str(expected.get("description", "")).strip(),
        origin=origin,
        sources=sources,
        payload=payload,
        expect=expectations,
        expected_skip=_transport_map(
            expected.get("expected_skip"), shape=shape, key="expected_skip"
        ),
        refused=_transport_map(expected.get("refused"), shape=shape, key="refused"),
        transform_file=transform_file,
        catalogue_intake=str(catalogue_intake) if catalogue_intake else None,
    )


def load_shapes(root: Path | None = None) -> tuple[FlowShape, ...]:
    """Every fixture shape, in directory-name order.

    Args:
        root: Fixture root, defaulting to the committed ``fixtures/`` tree.

    Returns:
        The parsed shapes.

    Raises:
        FixtureError: Any directory is malformed, or the tree is empty.
    """
    base = root or FIXTURES
    shapes = tuple(load_shape(d) for d in sorted(base.iterdir()) if d.is_dir())
    if not shapes:
        raise FixtureError(f"no flow fixtures under {base}")
    return shapes


def transports_for(requested: str) -> tuple[str, ...]:
    """The source transports one DFE_E2E_TRANSPORT value asks the suite to prove.

    Args:
        requested: ``kafka``, ``grpc`` or ``both``.

    Returns:
        The source-level transport names, in a stable order.

    Raises:
        ValueError: The value is not one of the three.
    """
    if requested == "both":
        return ("bus", "direct")
    try:
        return (DEPLOYMENT_TRANSPORTS[requested],)
    except KeyError:
        raise ValueError(
            f"DFE_E2E_TRANSPORT is {requested!r}; expected "
            f"{', '.join(sorted(DEPLOYMENT_TRANSPORTS))} or both"
        ) from None
