#  Project:      dfe-engine
#  File:         appmgmt/contract.py
#  Purpose:      Read an app's own container contract and resolve its config options
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What an app says its own configuration is, read off a mounted directory.

Every DFE Rust app emits its own contract - a JSON Schema of its config and a
catalogue of the capabilities it ships - and a deployment runs the pinned image
once at start to write that pair where the engine reads it. The contract is
therefore written by the exact binary that will read the config, so there is no
copy in this repo to drift against it.

Three answers come out of that pair: the contract as the app wrote it, the
contract flattened to one row per option carrying the value this instance would
run and where that value comes from, and whether a value about to be written is
one the app accepts. A deployment that mounts nothing answers
``available: false`` - the mount is dfe-infra's half of the wiring, and an engine
that has not been given one still serves every other route.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from dfe_engine.appmgmt.appconfig import CONFIG_ROOT, ENV_ROOT, custom_env
from dfe_engine.gitcrud.engine import flatten
from dfe_engine.manifest import ManifestError, manifest_path

CONTRACT_DIR_ENV = "DFE_APP_CONTRACT_DIR"
"""Where a deployment mounts the directory its apps emitted their contracts into."""

DEFAULT_CONTRACT_DIR = Path("/etc/dfe-engine/content/contract")
"""The conventional mount, so a chart that follows it needs no env var at all."""

SCHEMA_FILE = "config-schema.json"
CAPABILITIES_FILE = "capability-catalog.json"
SOURCE_FILE = "source.json"
"""scalo's own file names - the CLI chooses the directory and nothing else."""

SECRET_MARKER = "x-dfe-secret"
"""The schema keyword an app uses to mark a field as credential material."""

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
"""What a key under ``extraEnv`` may be called.

The whole point of the block is a name no contract declares, so the name is all
there is to check; the value is never judged against the app's schema.
"""

SECRET_NAMES = frozenset({"password", "secret", "token", "api_key", "private_key", "passphrase"})
"""Leaf names treated as secret whatever the schema says.

Half the fleet ships no ``x-dfe-secret`` at all - dfe-receiver, dfe-transform-vrl
and dfe-transform-vector carry none, including four plain-string passwords - so
trusting the marker alone would hand an operator's Kafka password back over the
API. The rule errs towards hiding.
"""

_TRANSFORM_CHART_ENV = {
    # dfe-common.transport resolves these from kafka.mode in the configmap, so the
    # app never sees what the overlay says about them.
    "source.transport": "dfe-common.transport",
    "sink.transport": "dfe-common.transport",
    "source.listen": "dfe-common.transport",
    "sink.endpoint": "dfe-common.transport",
    # The flat DFE_TRANSFORM_* env contract, which outranks the config file.
    "source.brokers": "DFE_TRANSFORM_SOURCE_BROKERS",
    "sink.brokers": "DFE_TRANSFORM_SINK_BROKERS",
    "source.topics": "DFE_TRANSFORM_SOURCE_TOPICS",
    "sink.topic": "DFE_TRANSFORM_SINK_TOPIC",
    "source.group_id": "DFE_TRANSFORM_SOURCE_GROUP_ID",
    "source.sasl.username": "DFE_TRANSFORM_SOURCE_SASL_USERNAME",
    "source.sasl.password": "DFE_TRANSFORM_SOURCE_SASL_PASSWORD",
    "sink.sasl.username": "DFE_TRANSFORM_SINK_SASL_USERNAME",
    "sink.sasl.password": "DFE_TRANSFORM_SINK_SASL_PASSWORD",
}

CHART_DERIVED: dict[str, dict[str, str]] = {
    "dfe-transform-vrl": _TRANSFORM_CHART_ENV,
    "dfe-transform-vector": _TRANSFORM_CHART_ENV,
    "dfe-loader": {
        "transport": "DFE_LOADER_TRANSPORT",
        "grpc.listen": "DFE_LOADER_GRPC__LISTEN",
        "kafka.brokers": "DFE_LOADER_KAFKA_BROKERS",
        "kafka.sasl.username": "DFE_LOADER_KAFKA_SASL_USERNAME",
        "kafka.sasl.password": "DFE_LOADER_KAFKA_SASL_PASSWORD",
        "clickhouse.hosts": "DFE_LOADER_CLICKHOUSE_HOSTS",
        "clickhouse.database": "DFE_LOADER_CLICKHOUSE_DATABASE",
        "clickhouse.username": "DFE_LOADER_CLICKHOUSE_USERNAME",
        "clickhouse.password": "DFE_LOADER_CLICKHOUSE_PASSWORD",
        # The pinned ClickHouse client has no TCP row fetch, so the chart holds
        # this app to http.
        "clickhouse.protocol": "DFE_LOADER_CLICKHOUSE__PROTOCOL",
        "routing.dlq.topic": "DFE_LOADER_DLQ_TOPIC",
        "routing.dlq.mode": "DFE_LOADER_DLQ_MODE",
    },
    "dfe-receiver": {
        "kafka.brokers": "DFE_RECEIVER_KAFKA_BROKERS",
        # The receiver's flat env spells the field USER where the loader spells it
        # USERNAME; both land on the same config path.
        "kafka.sasl.username": "DFE_RECEIVER_KAFKA_SASL_USER",
        "kafka.sasl.password": "DFE_RECEIVER_KAFKA_SASL_PASSWORD",
        "kafka.sasl.mechanism": "DFE_RECEIVER_KAFKA_SASL_MECHANISM",
        "server.bind_address": "DFE_RECEIVER_BIND_ADDRESS",
        "routing.dlq.enabled": "DFE_RECEIVER_DLQ_ENABLED",
        "routing.dlq.topic": "DFE_RECEIVER_DLQ_TOPIC",
        "routing.dlq.mode": "DFE_RECEIVER_DLQ_MODE",
    },
    "dfe-fetcher": {
        "kafka.sasl.username": "DFE_FETCHER_KAFKA_SASL_USER",
        "kafka.sasl.password": "DFE_FETCHER_KAFKA_SASL_PASSWORD",
        "kafka.sasl.mechanism": "DFE_FETCHER_KAFKA_SASL_MECHANISM",
        "dlq.enabled": "DFE_FETCHER_DLQ_ENABLED",
        "dlq.mode": "DFE_FETCHER_DLQ_MODE",
        # One env var sets both: naming a common topic also pins the routing to it.
        "dlq.kafka.common_topic": "DFE_FETCHER_DLQ_TOPIC",
        "dlq.kafka.routing": "DFE_FETCHER_DLQ_TOPIC",
    },
}
"""Config paths the dfe-infra chart decides, per app, and what decides each.

DATA rather than a rule, because which paths a chart derives is a property of the
charts and moves with them. A path named here that an app's schema does not carry
simply never matches a field.

The value is what an operator has to change instead - the flat env var the app
reads, or the chart helper that resolves it - so a refused write names the thing
that outranks the overlay rather than saying only that something does.
"""

_MAX_DEPTH = 25
"""Deeper than any shipped contract - the fetcher's, at five, is the deepest."""


class ContractError(ManifestError):
    """Raised when a mounted contract is there but cannot be read."""


class ContractSource(StrEnum):
    """Where a contract came from, which is a mount or nowhere."""

    MOUNT = "mount"
    ABSENT = "absent"


class Provenance(StrEnum):
    """What decides an option's value: the operator, the app, the chart, or nothing."""

    OVERLAY = "overlay"
    DEFAULT = "default"
    CHART = "chart"
    UNSET = "unset"


class _Missing:
    """Distinguishes an option with no value from one whose value is ``None``."""


MISSING = _Missing()


@dataclass(frozen=True)
class AppContract:
    """One app's contract, as the pinned image wrote it."""

    service: str
    available: bool
    source: ContractSource
    pinned_ref: str | None = None
    schema_version: str | None = None
    schema: dict[str, Any] = field(default_factory=dict)
    capabilities: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class ConfigField:
    """One config option, and the value this instance would run."""

    path: str
    type: str
    title: str
    description: str
    secret: bool
    enum: list[Any] | None
    default: Any
    value: Any
    provenance: Provenance
    dial: str | None
    protected: bool
    is_set: bool


@dataclass(frozen=True)
class UnknownEntry:
    """An overlay key under ``config:`` that the contract does not declare."""

    path: str
    value: Any


@dataclass(frozen=True)
class CustomEnvEntry:
    """An environment key the overlay carries beside the declared options."""

    path: str
    value: Any


@dataclass(frozen=True)
class DeclaredOption:
    """One declared option, reduced to what judging a written value needs."""

    path: str
    type: str
    enum: list[Any] | None
    nullable: bool


@dataclass(frozen=True)
class ConfigView:
    """Every option an instance has, plus the overlay keys none of them explain."""

    available: bool
    fields: list[ConfigField]
    unknown: list[UnknownEntry]
    custom: list[CustomEnvEntry] = field(default_factory=list)


# ── reading the mount ─────────────────────────────────────────


def contract_root(path: Path | str | None = None) -> Path | None:
    """Where contracts are read from: the argument, the environment, the mount."""
    return manifest_path(path, CONTRACT_DIR_ENV, DEFAULT_CONTRACT_DIR)


def _read_json(path: Path, *, what: str) -> Any:
    """Read one contract file.

    json rather than the yaml_load the rest of the manifest reading uses: scalo
    writes both spellings and the JSON half is the one to parse, the fetcher's
    schema alone being 167 KB.
    """
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ContractError(f"{what} {path} is not readable: {exc}") from exc


def load_contract(service: str, root: Path | str | None = None) -> AppContract:
    """The contract this deployment mounted for *service*, or that it mounted none.

    A directory with no subdirectory for the app, or a subdirectory with no
    schema, is a deployment whose emit step has not run or does not cover this
    app. That answers ``available: false`` rather than failing: the mount is
    dfe-infra's half of the wiring and every other route still works without it.
    """
    absent = AppContract(service=service, available=False, source=ContractSource.ABSENT)
    base = contract_root(root)
    if base is None:
        return absent
    schema_file = base / service / SCHEMA_FILE
    if not schema_file.is_file():
        return absent

    schema = _read_json(schema_file, what="app config schema")
    if not isinstance(schema, dict):
        raise ContractError(f"app config schema {schema_file} is not a mapping")

    capabilities_file = base / service / CAPABILITIES_FILE
    capabilities: list[Any] = []
    if capabilities_file.is_file():
        raw = _read_json(capabilities_file, what="app capability catalogue")
        if not isinstance(raw, list):
            raise ContractError(f"app capability catalogue {capabilities_file} is not a list")
        capabilities = raw

    pinned_ref = None
    source_file = base / service / SOURCE_FILE
    if source_file.is_file():
        emitted = _read_json(source_file, what="app contract source")
        if isinstance(emitted, dict):
            ref = emitted.get("ref")
            pinned_ref = str(ref) if ref else None

    return AppContract(
        service=service,
        available=True,
        source=ContractSource.MOUNT,
        pinned_ref=pinned_ref,
        # The only version in the contract: the apps all emit JSON Schema 2020-12
        # and nothing in the document names the app's own release.
        schema_version=schema.get("$schema"),
        schema=schema,
        capabilities=capabilities,
    )


_HELD: dict[str, AppContract] = {}


def contract(service: str, root: Path | str | None = None) -> AppContract:
    """The mounted contract, read once and held. An explicit root is never held."""
    if root is not None:
        return load_contract(service, root)
    if service not in _HELD:
        _HELD[service] = load_contract(service)
    return _HELD[service]


def reload_contracts() -> None:
    """Drop what is held, so a remounted directory takes effect."""
    _HELD.clear()


# ── the schema, flattened to options ──────────────────────────


def _deref(node: Any, root: dict, seen: tuple[str, ...] = ()) -> dict:
    """Follow ``$ref`` into ``$defs``, keeping the keywords beside the reference.

    A property that references a definition still carries its own description and
    its own whole-object default, and both outrank the target's.
    """
    while isinstance(node, dict) and "$ref" in node:
        ref = str(node["$ref"])
        beside = {k: v for k, v in node.items() if k != "$ref"}
        if not ref.startswith("#/") or ref in seen:
            return beside
        seen = (*seen, ref)
        target: Any = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part, {}) if isinstance(target, dict) else {}
        node = {**target, **beside} if isinstance(target, dict) else beside
    return node if isinstance(node, dict) else {}


def _merged(node: dict, root: dict) -> dict:
    """One node with every non-null branch's properties folded in.

    An optional nested block is spelled ``anyOf: [<the block>, null]``, and the
    console has to show what is inside it rather than one box holding an object.
    """
    out = {k: v for k, v in node.items() if k not in ("anyOf", "oneOf", "allOf")}
    props = dict(node.get("properties") or {})
    for keyword in ("anyOf", "oneOf", "allOf"):
        for branch in node.get(keyword) or ():
            resolved = _deref(branch, root)
            if resolved.get("type") == "null":
                continue
            props.update(resolved.get("properties") or {})
    if props:
        out["properties"] = props
    elif any(k in node for k in ("anyOf", "oneOf", "allOf")):
        # No branch carries properties, so the union is a leaf and the branches
        # are still what says what it accepts.
        return node
    return out


def _type_of(node: dict, root: dict) -> str:
    """The type to render, with the null half of an optional dropped."""
    declared = node.get("type")
    if isinstance(declared, str):
        return declared
    if isinstance(declared, list):
        return next((str(t) for t in declared if t != "null"), "null")
    for keyword in ("anyOf", "oneOf"):
        for branch in node.get(keyword) or ():
            resolved = _deref(branch, root)
            if resolved.get("type") and resolved["type"] != "null":
                declared = resolved["type"]
                return declared if isinstance(declared, str) else str(declared[0])
    return ""


def _enum_of(node: dict, root: dict) -> list[Any] | None:
    """The values this option accepts, in either spelling the apps use.

    ``enum`` is rare in the shipped contracts; a Rust enum comes out as a
    ``oneOf`` of ``const`` branches instead, and that is a dropdown too.
    """
    declared = node.get("enum")
    if isinstance(declared, list) and declared:
        return list(declared)
    for keyword in ("oneOf", "anyOf"):
        branches = node.get(keyword) or ()
        consts = []
        for branch in branches:
            resolved = _deref(branch, root)
            if resolved.get("type") == "null":
                continue
            if "const" not in resolved:
                consts = []
                break
            consts.append(resolved["const"])
        if consts:
            return consts
    return None


def _nullable(node: dict, root: dict) -> bool:
    """Whether the app accepts an explicit null here.

    An optional field is spelled ``anyOf: [<the type>, null]``, so refusing every
    null would refuse clearing a field the app itself declares as clearable.
    """
    declared = node.get("type")
    if isinstance(declared, str):
        return declared == "null"
    if isinstance(declared, list):
        return "null" in declared
    for keyword in ("anyOf", "oneOf"):
        for branch in node.get(keyword) or ():
            if _deref(branch, root).get("type") == "null":
                return True
    return False


def _is_secret(path: str, node: dict) -> bool:
    return bool(node.get(SECRET_MARKER)) or path.rsplit(".", 1)[-1] in SECRET_NAMES


def walk_schema(schema: dict) -> Iterator[tuple[str, dict, Any]]:
    """Every leaf option in a config schema, as ``(path, node, inherited default)``.

    A node with properties is a section rather than an option, so only the leaves
    are yielded. An array is a leaf whatever its items are: its value is the whole
    list, and no dot path addresses one element of it.
    """

    def descend(node: Any, path: str, inherited: Any, depth: int) -> Iterator[tuple]:
        resolved = _merged(_deref(node, schema), schema)
        props = resolved.get("properties")
        if isinstance(props, dict) and props and depth < _MAX_DEPTH:
            own = resolved.get("default")
            for name, child in props.items():
                if isinstance(own, dict):
                    below = own.get(name, MISSING)
                elif isinstance(inherited, dict):
                    below = inherited.get(name, MISSING)
                else:
                    below = MISSING
                yield from descend(child, f"{path}.{name}" if path else name, below, depth + 1)
            return
        yield path, resolved, inherited

    yield from descend(schema, "", MISSING, 0)


def catalogue_enums(capabilities: list[Any]) -> dict[str, list[Any]]:
    """Config paths the capability catalogue constrains, keyed by dotted path.

    The schemas carry almost no constraint - the loader declares
    ``clickhouse.protocol`` as a plain string - while the catalogue names the two
    values it accepts and says which one is refused at startup. The catalogue is
    the better answer, so where it names a field it wins.
    """
    out: dict[str, list[Any]] = {}

    def visit(entries: Any, prefix: str) -> None:
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", ""))
            if not name:
                continue
            path = f"{prefix}.{name}" if prefix else name
            for spec in entry.get("fields") or ():
                if not isinstance(spec, dict):
                    continue
                values = spec.get("enum_values")
                if isinstance(values, list) and values and spec.get("name"):
                    out[f"{path}.{spec['name']}"] = list(values)
            visit(entry.get("children"), path)

    visit(capabilities, "")
    return out


# ── judging a value before it is committed ────────────────────

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _json_type(value: Any) -> str:
    """The JSON Schema name for a value, so a refusal reads in the schema's words."""
    # bool first, since Python's bool IS an int and would otherwise read "integer".
    if isinstance(value, bool):
        return "boolean"
    for name, types in _JSON_TYPES.items():
        if name != "boolean" and isinstance(value, types):
            return name
    return "null"


def declared_options(app_contract: AppContract) -> dict[str, DeclaredOption]:
    """Every option the contract declares, keyed by the overlay path a write uses."""
    enums = catalogue_enums(app_contract.capabilities)
    out: dict[str, DeclaredOption] = {}
    for path, node, _inherited in walk_schema(app_contract.schema):
        if not path:
            continue
        out[f"{CONFIG_ROOT}.{path}"] = DeclaredOption(
            path=f"{CONFIG_ROOT}.{path}",
            type=_type_of(node, app_contract.schema),
            enum=enums.get(path) or _enum_of(node, app_contract.schema),
            nullable=_nullable(node, app_contract.schema),
        )
    return out


def check_value(option: DeclaredOption, value: Any) -> str:
    """Why the app would refuse this value, or empty when it accepts it.

    The apps declare a type everywhere and a closed set almost nowhere, so a value
    that passes here can still be one the app rejects at startup.
    """
    if value is None:
        return "" if option.nullable else f"{option.path} does not accept a null"
    accepted = _JSON_TYPES.get(option.type)
    if accepted is not None:
        # A bool is an int in Python and is not one here: `true` in a chunk-size
        # field would otherwise be written as 1.
        wrong = not isinstance(value, accepted) or (
            isinstance(value, bool) and option.type != "boolean"
        )
        if wrong:
            return f"{option.path} takes {option.type}, not {_json_type(value)}"
    if option.enum is not None and value not in option.enum:
        offered = ", ".join(repr(v) for v in option.enum)
        return f"{option.path} takes one of {offered}, not {value!r}"
    return ""


def check_env_value(value: Any) -> str:
    """Why this value cannot become an environment entry, or empty when it can."""
    if isinstance(value, (dict, list)):
        return "an environment value is one scalar, not a list or a mapping"
    if isinstance(value, str) and ("\n" in value or "\r" in value):
        return "an environment value carries no line break: a second line is a second key"
    return ""


def chart_supplier(service: str, path: str) -> str | None:
    """What the deployment sets this option with, or None where it sets nothing.

    ``path`` is the overlay path, ``config.`` rooted, so a caller compares the
    request's own keys rather than re-deriving them.
    """
    inner = path.split(".", 1)[1] if path.startswith(f"{CONFIG_ROOT}.") else path
    return CHART_DERIVED.get(service, {}).get(inner)


# ── resolving one instance's values ───────────────────────────


def _at(doc: Any, path: str) -> Any:
    """The overlay's value at a dotted path, or MISSING when it carries none."""
    node = doc
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return MISSING
        node = node[part]
    return node


def _declares(path: str, leaves: set[str]) -> bool:
    """Whether some option in the contract explains this flattened overlay key."""
    parts = path.split(".")
    for depth in range(1, len(parts) + 1):
        prefix = ".".join(parts[:depth])
        if prefix in leaves or prefix.split("[", 1)[0] in leaves:
            return True
    return False


def resolve_config(
    app_contract: AppContract,
    overlay: dict,
    *,
    is_protected: Callable[[str], bool] | None = None,
) -> ConfigView:
    """Every option this instance has, with its value and where the value comes from.

    Paths are the overlay's own - ``config.<schema path>`` - so what this reports
    is what a write addresses.

    A chart-derived path is reported as such before the overlay is consulted: the
    deployment decides it, and telling the console an overlay key governs a broker
    list the DFE_TRANSFORM_* env contract overrides is the answer this provenance
    exists to prevent.
    """
    block = overlay.get(CONFIG_ROOT)
    block = block if isinstance(block, dict) else {}
    derived = CHART_DERIVED.get(app_contract.service, {})
    enums = catalogue_enums(app_contract.capabilities)
    protected = is_protected or (lambda _path: False)

    fields: list[ConfigField] = []
    leaves: set[str] = set()
    for path, node, inherited in walk_schema(app_contract.schema):
        if not path:
            continue
        leaves.add(path)
        full = f"{CONFIG_ROOT}.{path}"
        written = _at(block, path)
        default = node["default"] if "default" in node else inherited
        has_default = not isinstance(default, _Missing)
        secret = _is_secret(path, node)

        if path in derived:
            provenance = Provenance.CHART
        elif not isinstance(written, _Missing):
            provenance = Provenance.OVERLAY
        elif has_default:
            provenance = Provenance.DEFAULT
        else:
            provenance = Provenance.UNSET

        value = written if not isinstance(written, _Missing) else default
        fields.append(
            ConfigField(
                path=full,
                type=_type_of(node, app_contract.schema),
                title=str(node.get("title", "")),
                description=str(node.get("description", "")),
                secret=secret,
                enum=enums.get(path) or _enum_of(node, app_contract.schema),
                # A marked secret's schema default is the literal ***REDACTED***,
                # which rendered as a placeholder tells an operator to type around it.
                default=None if secret else (default if has_default else None),
                value=None if secret else (value if not isinstance(value, _Missing) else None),
                provenance=provenance,
                dial=None,
                protected=protected(full),
                # Written here, or supplied by the chart: either way the instance
                # runs a value, which is all a secret is allowed to report.
                is_set=not isinstance(written, _Missing) or provenance is Provenance.CHART,
            )
        )

    unknown = [
        UnknownEntry(path=f"{CONFIG_ROOT}.{path}", value=value)
        for path, value in flatten(block).items()
        if not _declares(path, leaves)
    ]
    # Apart from `unknown`, which is an overlay key that has outrun its contract;
    # an extraEnv key is outside every contract on purpose.
    custom = [
        CustomEnvEntry(path=f"{ENV_ROOT}.{key}", value=value)
        for key, value in sorted(custom_env(overlay).items())
    ]
    return ConfigView(
        available=app_contract.available, fields=fields, unknown=unknown, custom=custom
    )
