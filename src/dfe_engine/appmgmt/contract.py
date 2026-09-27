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

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from scalo.logger.filters import MASK_VALUE, SENSITIVE_FIELDS

from dfe_engine.appmgmt.appconfig import CONFIG_ROOT, ENV_ROOT, custom_env
from dfe_engine.appmgmt.catalogue import APP_CATALOGUE, DEPLOY_SERVICE_PATH
from dfe_engine.gitcrud.engine import flatten, get_path, set_path
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

_NAME_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
"""One word of a name: an acronym, a capitalised or lower-case run, or digits.

Every other character separates words, so ``bearer-tokens``, ``bearer_tokens``,
``bearerTokens`` and ``BEARER_TOKENS`` all read as ``bearer``, ``tokens``.
"""


def _words(name: str) -> tuple[str, ...]:
    """A name as lower-case words, split at separators and camelCase humps."""
    return tuple(word.lower() for word in _NAME_WORD.findall(name))


_FLOOR_TERMS = frozenset(
    {
        "access_key",
        "ssh_key",
        "tls_key",
        "aws_secret",
        "gcp_key",
        "azure_key",
        "session",
        "cookie",
        "csrf",
        "private",
        "ssn",
        "tfn",
        "medicare",
        "credit_card",
        "card_number",
        "cvv",
        "email",
        "phone",
    }
)
"""The standard's sensitive-field floor, less what scalo-py's list already holds.

``address`` is left out: in an app's config it names a listen or connect address.
"""

_ENGINE_TERMS = frozenset({"passphrase", "jaas_config", "credential"})
"""Credentials the DFE apps and their brokers name that neither list above covers.

``credential`` is here because a term's plural matches and its singular does not.
"""

CREDENTIAL_TERMS: frozenset[tuple[str, ...]] = frozenset(
    _words(term.replace("_", " ")) for term in {*SENSITIVE_FIELDS, *_FLOOR_TERMS, *_ENGINE_TERMS}
)
"""Every term that makes a name a credential when the name ends in it, as words.

scalo-py's own ``SENSITIVE_FIELDS``, extended by :data:`_FLOOR_TERMS` and
:data:`_ENGINE_TERMS`. ``sasl_password``, ``auth-token``, ``rootPassword`` and
``KAFKA_SASL_JAAS_CONFIG`` each end in one; ``token_url`` and ``password_field``
do not. A trailing ``s`` is read as the plural of the term's last word.

Part of the fleet ships no ``x-dfe-secret`` at all, so trusting the marker alone
would hand an operator's Kafka password back over the API. The rule errs towards
hiding, and holds for an app that has not adopted the marker yet.
"""

SECTION_TERMS: frozenset[tuple[str, ...]] = frozenset(
    _words(term.replace("_", " "))
    for term in {
        "auth",
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "cert",
        "certificate",
        "ssl_cert",
        "session",
        "cookie",
        "csrf",
        "private",
        "email",
        "phone",
    }
)
"""Terms that name a section as often as a value: a mapping under one is read field by field.

``server.auth`` holds the auth mode beside its bearer tokens, so masking the whole
mapping for its name would hide settings. A scalar ``REDISCLI_AUTH`` is still masked.
"""

CREDENTIAL_MAP = ("credentials",)
"""A mapping whose name ends in this word holds credentials by name, so each value is one.

dfe-fetcher's ``auth.credentials`` maps a name to the credential its placements read.
A list of that name holds entries instead, and each entry's fields are judged by name.
"""

FORM_WORDS = frozenset({"hash", "hashes", "digest", "digests", "json", "pem"})
"""A last word saying what form a value is held in, so the words before it are judged.

``password_hash`` and ``seeded_password_hash`` are crackable digests of a password,
``credentials_json`` is a credential document and librdkafka's ``ssl.key.pem`` is a
private key; ``config_hash`` and ``capture_json`` stay shown.
"""

_COMPACT_STEMS = (
    "password",
    "passwords",
    "passwd",
    "secret",
    "secrets",
    "token",
    "tokens",
    "apikey",
    "passphrase",
)
"""Credential words run together into a name's last word, as in ``PGPASSWORD``.

Judged only for a scalar, since ``onepassword`` is also the name of a source section.
"""

_SHOWN_ENDINGS: frozenset[tuple[str, ...]] = frozenset({("client", "auth")})
"""Name endings that match a credential term and hold a setting: the TLS client-auth mode."""

KEY_QUALIFIERS = frozenset(
    {
        "api",
        "private",
        "secret",
        "access",
        "account",
        "license",
        "licence",
        "signing",
        "encryption",
        "hmac",
        "master",
        "shared",
        "ssh",
        "tls",
        "ssl",
        "client",
        "auth",
        "sasl",
        "credential",
        "credentials",
        "jwt",
        "gcp",
        "azure",
        "session",
        "cookie",
        "csrf",
    }
)
"""Words that make a ``key`` a credential when they qualify it.

The qualifier is the word before ``key`` in the name - ``secret_access_key``,
``SIGNING_KEY``, ``JWT_KEY`` - or, for a bare ``key``, the section holding it, as
``tls.key``. A ``key`` nothing qualifies is as often a routing or partition key -
``KAFKA_PARTITION_KEY``, ``sink.key_field`` - and stays shown, as ``FOO_KEY`` does.
``ssl`` is librdkafka's spelling of ``tls``.
"""

VALUE_QUALIFIERS = frozenset({"header", "headers"})
"""Words that make a ``value`` a credential when they qualify it, as for a ``key``.

An accepted auth header's values are what a client must present: dfe-receiver's
``auth.header_values`` and each ``auth.accepted_headers[].values``. A ``value`` under
a credential word, as ``token.value``, is one too; the loader's DLQ ``value`` is not.
"""

SECRET_SOURCE_WORDS = frozenset(
    {"credential", "credentials", "ca", "cert", "key", "config", "existing", "pull"}
)
"""Words that make a name ending in ``secret`` a secret source, not a secret.

It names where a credential is fetched from, which an operator needs to see:
dfe-fetcher's ``credential_secret`` and ``private_key_secret`` and dfe-receiver's
``tls.key_secret`` each hold a ``provider:path:key`` spec such as
``vault:kv/data/github:token``; a chart's ``config_secret``, ``existingSecret`` and
``imagePullSecrets`` name Kubernetes Secrets.
"""

_SECRET_KEY_NAMES = ("secret", "keys")
_SECRET_REFERENCE = ("existing", "secret")
"""A chart's ``secretKeys`` beside ``existingSecret``: which key of that Secret holds each value.

Every DFE chart sets the pair together, and the map holds key names, not credentials.
"""

REDACTED = MASK_VALUE
"""What stands in for a credential in a document read back over the API.

scalo's own mask, so an operator sees one spelling for a hidden value wherever it
is shown.
"""

_URL_PASSWORD = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@]*:)([^\s/]+)(?=@)")
"""The password in a URL's ``user:password@`` userinfo.

It runs to the last ``@`` before the path, so a password holding one is masked whole.
"""

IDENTITY_KEYS = ("id", "name")
"""Fields that name a list entry, so a masked entry finds the stored one it was read from.

dfe-fetcher requires an ``id`` on each of its ``connections``; dfe-receiver names
each accepted header by ``name``.
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
    "dfe-archiver": {
        # Bare KAFKA_*/ARCHIVER_*/S3_*/DLQ_*, this app's own flat-env contract
        # (core config.rs), not the DFE_ARCHIVER_* family the other apps use.
        "transport": "ARCHIVER_TRANSPORT",
        "grpc.listen": "ARCHIVER_GRPC_LISTEN",
        "kafka.brokers": "KAFKA_BROKERS",
        "kafka.security_protocol": "KAFKA_SECURITY_PROTOCOL",
        "kafka.sasl_username": "KAFKA_SASL_USER",
        "kafka.sasl_password": "KAFKA_SASL_PASSWORD",
        "kafka.sasl_mechanism": "KAFKA_SASL_MECHANISM",
        "kafka.topic_include": "KAFKA_TOPIC_INCLUDE",
        "archive.destination": "ARCHIVER_DESTINATION",
        "archive.s3.endpoint": "S3_ENDPOINT",
        "archive.s3.bucket": "S3_BUCKET",
        "dlq.enabled": "DLQ_ENABLED",
        "dlq.mode": "DLQ_MODE",
        # One env var sets both: naming a common topic also pins the routing to it.
        "dlq.kafka.common_topic": "DLQ_TOPIC",
        "dlq.kafka.routing": "DLQ_TOPIC",
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

_TRANSFORM_CHART_GATES = {
    "source.topics": "kafka.sourceTopic",
    "sink.topic": "kafka.destTopic",
}

CHART_DERIVED_WHEN: dict[str, dict[str, str]] = {
    "dfe-transform-vrl": _TRANSFORM_CHART_GATES,
    "dfe-transform-vector": _TRANSFORM_CHART_GATES,
}
"""Chart-derived paths the chart sets only while one of its own values is set.

Each maps to that value's overlay path. The transform charts render
``DFE_TRANSFORM_SOURCE_TOPICS`` and ``DFE_TRANSFORM_SINK_TOPIC`` inside a
``with`` on ``kafka.sourceTopic`` and ``kafka.destTopic``, both empty by default,
so an instance whose overlay leaves them empty reads the topics from its own
config file. A value set for every instance by a profile, outside the overlay, is
not seen here.
"""

_MAX_DEPTH = 25
"""Deeper than any shipped contract - the fetcher's, at five, is the deepest."""


class ContractError(ManifestError):
    """Raised when a mounted contract is there but cannot be read."""


class MaskedValueError(ValueError):
    """Raised when a write carries the redaction placeholder where nothing is stored."""

    code = "masked_value"
    """The refusal code a route answers with."""


class CredentialReentryError(MaskedValueError):
    """Raised when a masked credential is written back beside a field that changed.

    Restoring it would send the stored credential wherever the changed field now
    points, so the caller types the credential again.
    """

    code = "credential_reentry_required"


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


# --- reading the mount ---


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


# --- the schema, flattened to options ---


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


def _branches(node: Any, root: dict) -> list[dict]:
    """A node and every non-null branch of it, each resolved.

    An optional value is spelled ``anyOf: [<the value>, null]``, so what it holds -
    its items, its fields, a secret marker - sits on a branch rather than the node.
    """
    resolved = _deref(node, root)
    out = [resolved]
    for keyword in ("anyOf", "oneOf", "allOf"):
        for branch in resolved.get(keyword) or ():
            target = _deref(branch, root)
            if target.get("type") != "null":
                out.append(target)
    return out


def _qualified(words: tuple[str, ...], section: str, noun: str, qualifiers: frozenset[str]) -> bool:
    """Whether a name ending in ``noun`` is a credential, per the word qualifying it.

    The qualifier is the word before ``noun``, or for a bare ``noun`` the last word
    of ``section``. It counts when it is in ``qualifiers`` or is a credential term.
    """
    if words[-1] not in (noun, f"{noun}s"):
        return False
    if len(words) > 1:
        qualifier = words[-2]
    else:
        section_words = _words(section)
        qualifier = section_words[-1] if section_words else ""
    return qualifier in qualifiers or (qualifier,) in CREDENTIAL_TERMS


def _ends_in(words: tuple[str, ...], term: tuple[str, ...]) -> bool:
    """Whether ``words`` ends in ``term``, or in its plural."""
    size = len(term)
    if len(words) < size or words[-size:-1] != term[:-1]:
        return False
    return words[-1] in (term[-1], f"{term[-1]}s")


def secret_name(name: str, section: str = "", *, mapping: bool = False) -> bool:
    """Whether a name says the value under it is a credential.

    The name ends in one of :data:`CREDENTIAL_TERMS` - ``auth_token``,
    ``bearer-tokens``, ``rootPassword`` - unless it is a secret source per
    :data:`SECRET_SOURCE_WORDS` or one of :data:`_SHOWN_ENDINGS`; or it is a ``key``
    or a ``value`` a credential word qualifies, where ``section``, the name of the
    mapping holding it, qualifies a bare one. A scalar whose last word runs a
    credential word in, as ``PGPASSWORD`` does, is one too. A last word in
    :data:`FORM_WORDS` is dropped first, so ``password_hash`` is judged as ``password``.

    ``mapping`` says the value is a mapping or a list of them, which a name in
    :data:`SECTION_TERMS` does not hide whole: its fields are judged one by one.
    """
    words = _words(name)
    while len(words) > 1 and words[-1] in FORM_WORDS:
        words = words[:-1]
    if not words:
        return False
    if _qualified(words, section, "key", KEY_QUALIFIERS) or _qualified(
        words, section, "value", VALUE_QUALIFIERS
    ):
        return True
    matched = [term for term in CREDENTIAL_TERMS if _ends_in(words, term)]
    if matched:
        if any(_ends_in(words, ending) for ending in _SHOWN_ENDINGS):
            return False
        if all(term == ("secret",) for term in matched) and len(words) > 1:
            if words[-2] in SECRET_SOURCE_WORDS:
                return False
        return not mapping or any(term not in SECTION_TERMS for term in matched)
    return not mapping and words[-1].endswith(_COMPACT_STEMS)


def _object_branch(branch: dict) -> bool:
    """Whether one resolved schema branch describes a mapping."""
    declared = branch.get("type")
    typed = declared == "object" or (isinstance(declared, list) and "object" in declared)
    return typed or "properties" in branch or isinstance(branch.get("additionalProperties"), dict)


def _takes_fields(node: Any, root: dict) -> bool:
    """Whether a value of this schema is a mapping, or a list of mappings."""
    for branch in _branches(node, root):
        if _object_branch(branch):
            return True
        items = branch.get("items")
        if isinstance(items, dict) and any(_object_branch(b) for b in _branches(items, root)):
            return True
    return False


def _has_fields(value: Any) -> bool:
    """Whether a value is a mapping, or a list holding one."""
    if isinstance(value, dict):
        return True
    return isinstance(value, list) and any(isinstance(item, dict) for item in value)


def _wholly_secret(path: str, node: dict, root: dict) -> bool:
    """Whether an option's whole value is credential material, not just fields inside it.

    Its name says so, its schema marks it, or it lists values each marked secret, as
    bearer tokens are. A map or list of objects with credential fields is not: its
    other fields are settings an operator needs to see.
    """
    parts = path.split(".")
    section = parts[-2] if len(parts) > 1 else ""
    if secret_name(parts[-1], section, mapping=_takes_fields(node, root)):
        return True
    for branch in _branches(node, root):
        if branch.get(SECRET_MARKER):
            return True
        items = branch.get("items")
        if isinstance(items, dict) and any(b.get(SECRET_MARKER) for b in _branches(items, root)):
            return True
    return False


def _masked(value: Any) -> Any:
    """The redaction in the shape of what it replaces, so a list stays a list.

    Nothing to hide stays as it is: a null or an empty string says only that no
    credential was written.
    """
    if value is None or value == "":
        return value
    if isinstance(value, list):
        return [_masked(item) for item in value]
    return REDACTED


def _shown_text(text: str) -> str:
    """``text`` with the password of every ``scheme://user:password@`` URL in it masked."""
    return _URL_PASSWORD.sub(rf"\1{REDACTED}", text)


def _secret_key_names(name: str, value: Any, holder: dict) -> bool:
    """Whether ``value``, held under ``name`` in ``holder``, is a chart's ``secretKeys``."""
    return (
        _words(name) == _SECRET_KEY_NAMES
        and isinstance(value, dict)
        and all(isinstance(item, str) for item in value.values())
        and any(_words(str(key)) == _SECRET_REFERENCE for key in holder)
    )


def _redact(
    value: Any, node: Any, root: dict, name: str, section: str = "", *, entry: bool = False
) -> Any:
    """``value`` with every part its schema marks, or its name calls, a secret masked.

    ``node`` is the schema the value was written against, or None where nothing
    declares it, which leaves only the name rule to judge it. ``section`` is the
    name of the mapping holding ``value``, and ``entry`` says ``value`` is an entry
    of a list of that name. Text that survives both carries its URL passwords
    masked, as a connection string does.

    A mapping named per :data:`CREDENTIAL_MAP` has each value masked, and a chart's
    ``secretKeys`` beside ``existingSecret`` is shown: it names keys, not credentials.
    """
    if isinstance(value, _Missing):
        return value
    branches = _branches(node, root) if node is not None else []
    if any(b.get(SECRET_MARKER) for b in branches) or secret_name(
        name, section, mapping=_has_fields(value)
    ):
        return _masked(value)
    if isinstance(value, str):
        return _shown_text(value)
    if isinstance(value, dict):
        props: dict[str, Any] = {}
        extra: Any = None
        for branch in branches:
            props.update(branch.get("properties") or {})
            if extra is None and isinstance(branch.get("additionalProperties"), dict):
                extra = branch["additionalProperties"]
        by_name = not entry and _ends_in(_words(name), CREDENTIAL_MAP)
        out: dict[Any, Any] = {}
        for key, child in value.items():
            if _secret_key_names(str(key), child, value):
                out[key] = {item: _shown_text(text) for item, text in child.items()}
            elif by_name and not _has_fields(child):
                out[key] = _masked(child)
            else:
                out[key] = _redact(child, props.get(key, extra), root, str(key), name)
        return out
    if isinstance(value, list):
        items = next(
            (b["items"] for b in branches if isinstance(b.get("items"), dict)),
            None,
        )
        # An entry is judged under the list's own name, so its fields have a section.
        return [_redact(child, items, root, name, section, entry=True) for child in value]
    return value


def secret_env_name(name: str) -> bool:
    """Whether an environment name says it carries a credential.

    :func:`secret_name` for a scalar: ``DFE_LOADER_KAFKA_SASL_PASSWORD``,
    ``S3_API_KEY``, ``KAFKA_SASL_JAAS_CONFIG``, ``HTTP_AUTHORIZATION`` and
    ``PGPASSWORD`` each say so. A trailing ``KEY`` counts only where
    :data:`KEY_QUALIFIERS` qualifies it, so ``S3_SECRET_KEY`` is masked and
    ``KAFKA_PARTITION_KEY`` is not.
    """
    return secret_name(name)


def _redact_env(env: Any) -> Any:
    """An environment block with every credential it carries masked.

    A value whose name says it is one is masked whole. Any other keeps its text,
    less the password of a URL in it, as ``DATABASE_URL`` would carry.
    """
    if not isinstance(env, dict):
        return env
    return {
        key: _masked(value)
        if secret_env_name(str(key))
        else (_shown_text(value) if isinstance(value, str) else value)
        for key, value in env.items()
    }


def redact_overlay(app_contract: AppContract, overlay: dict) -> dict:
    """The overlay with every credential it carries replaced by :data:`REDACTED`.

    The ``config:`` block is read against the app's own schema, so a value the app
    marks secret is hidden whatever it is called. An ``extraEnv`` entry is judged by
    :func:`secret_env_name`. Every other key, and everything when no contract is
    mounted, is judged by name alone.
    """
    schema = app_contract.schema if app_contract.available else {}
    out: dict = {}
    for key, value in overlay.items():
        if key == ENV_ROOT:
            out[key] = _redact_env(value)
        else:
            node = schema if key == CONFIG_ROOT and schema else None
            out[key] = _redact(value, node, schema, str(key))
    return out


def redact_resource(doc: dict) -> dict:
    """Any deploy-repo document with its credentials masked.

    An app overlay names its app at ``deploy.service`` and is read against that
    app's mounted contract. A document naming no app the manifest declares is
    judged by name alone, so a crafted name never picks the file that is read.

    Raises:
        ContractError: The named app's mounted contract is there but unreadable.
    """
    service = _at(doc, DEPLOY_SERVICE_PATH)
    if isinstance(service, str) and service in APP_CATALOGUE:
        found = contract(service)
    else:
        found = AppContract(service="", available=False, source=ContractSource.ABSENT)
    return redact_overlay(found, doc)


def shown_var(doc: dict, path: str, value: Any) -> Any:
    """:func:`redact_var` for text that leaves the engine, where it must never fail.

    An unreadable contract mount leaves the name rule to judge the value, so a
    broken mount cannot block the write that is being described.
    """
    try:
        return redact_var(doc, path, value)
    except ContractError:
        return redact_var({}, path, value)


_PROBE = "probe"
"""A stand-in value, so a path is judged by where it points and not by what it holds."""


def credential_var(doc: dict, path: str, value: Any = MISSING) -> bool:
    """Whether a write of ``value`` at ``path`` in ``doc`` would store a credential.

    True where the path itself is one - its name, the schema's marker, a list of
    marked values - whatever the value, and where ``value`` carries a credential
    field of its own.
    """
    probes: list[Any] = [_PROBE, [_PROBE]]
    if not isinstance(value, _Missing):
        probes.append(value)
    return any(shown_var(doc, path, probe) != probe for probe in probes)


def shown_resource(doc: dict) -> dict:
    """:func:`redact_resource` for an error body, where it must never fail.

    An unreadable contract mount leaves the name rule to judge the document.
    """
    try:
        return redact_resource(doc)
    except ContractError:
        return redact_overlay(
            AppContract(service="", available=False, source=ContractSource.ABSENT), doc
        )


def _carries_mask(value: Any) -> bool:
    """Whether :data:`REDACTED` stands anywhere in ``value``, a URL password included."""
    if isinstance(value, str):
        return REDACTED in value
    if isinstance(value, list):
        return any(_carries_mask(item) for item in value)
    if isinstance(value, dict):
        return any(_carries_mask(item) for item in value.values())
    return False


def _named(entries: list, key: str) -> dict[Any, Any] | None:
    """Each entry by its ``key``, or None where one lacks it or two share it."""
    found: dict[Any, Any] = {}
    for entry in entries:
        name = entry.get(key) if isinstance(entry, dict) else None
        if not isinstance(name, str | int) or isinstance(name, bool):
            return None
        if name == REDACTED or name in found:
            return None
        found[name] = entry
    return found


def _same_but_masked(value: Any, stored: Any, shown: Any = MISSING) -> bool:
    """Whether ``value`` is ``stored`` as a read shows it: equal wherever it is not masked.

    A value the read showed only as a credential may be typed again, and a URL whose
    password alone was masked may take a new password, so neither is compared to what
    is stored: comparing them would answer a guess at the stored credential
    differently from a wrong one, which tells a writer who cannot read it that the
    guess was right. ``shown`` is ``stored`` as the read showed it, where the caller
    has it.
    """
    if _wholly_masked(shown):
        return True
    if isinstance(shown, str) and REDACTED in shown:
        return isinstance(value, str) and _shown_text(value) == shown
    if isinstance(value, str) and value == REDACTED:
        return stored is not None and not isinstance(stored, _Missing)
    if isinstance(value, str) and REDACTED in value:
        return isinstance(stored, str) and _shown_text(stored) == value
    if isinstance(value, dict):
        return isinstance(stored, dict) and all(
            _same_but_masked(item, stored.get(key, MISSING), _shown_child(shown, key))
            for key, item in value.items()
        )
    if isinstance(value, list):
        if not isinstance(stored, list) or len(value) != len(stored):
            return False
        views = shown if isinstance(shown, list) and len(shown) == len(stored) else None
        return all(
            _same_but_masked(item, stored[i], views[i] if views else MISSING)
            for i, item in enumerate(value)
        )
    return value == stored


def _views(kept: list, shown: Any) -> list[tuple[Any, Any]]:
    """Each stored entry beside the form a read showed it in.

    With no read to go on, the entry itself stands in. A read that showed the list
    as anything but a list of the same length showed no entry at all.
    """
    if isinstance(shown, _Missing):
        return [(entry, entry) for entry in kept]
    if isinstance(shown, list) and len(shown) == len(kept):
        return list(zip(kept, shown, strict=True))
    return [(entry, REDACTED) for entry in kept]


def _restore_list(value: list, stored: Any, shown: Any, path: str) -> list:
    """A written list with each masked entry matched to the stored entry it was read from.

    Entries named by one of :data:`IDENTITY_KEYS` are matched by name, so deleting
    or reordering one cannot hand its credential to a neighbour. Unnamed entries are
    matched in stored order, and only while every stored entry is accounted for and
    each masked one is otherwise unchanged; anything else cannot be told apart.

    A written entry accounts for a stored one only when it matches the entry as a read
    showed it, so a credential typed back in the clear is counted as new and a guess
    at a stored token is refused whether it is right or wrong.
    """
    kept = stored if isinstance(stored, list) else []
    pairs = _views(kept, shown)
    masked = [item for item in value if _carries_mask(item)]
    for key in IDENTITY_KEYS:
        wanted, by_name = _named(masked, key), _named(kept, key)
        if wanted is not None and by_name is not None:
            seen = {entry[key]: view for entry, view in pairs}
            return [
                restore_masked(
                    item,
                    by_name.get(item[key], MISSING),
                    path=f"{path}[{i}]",
                    shown=seen.get(item[key], MISSING),
                )
                if _carries_mask(item)
                else item
                for i, item in enumerate(value)
            ]
    remaining = list(pairs)
    for item in value:
        if _carries_mask(item):
            continue
        match = next((pair for pair in remaining if pair[1] == item), None)
        if match is not None:
            remaining.remove(match)
    if not remaining:
        return [restore_masked(item, MISSING, path=f"{path}[{i}]") for i, item in enumerate(value)]
    where = path or "the list"
    if len(masked) > len(remaining):
        raise MaskedValueError(
            f"{where} has {len(masked)} masked entries for {len(remaining)} stored, so a "
            "placeholder stands where nothing is stored: write the credential itself"
        )
    if len(masked) < len(remaining):
        raise MaskedValueError(
            f"{where} has {len(masked)} masked entries for {len(remaining)} stored and "
            "nothing names them, so which were removed cannot be told: write the "
            "list's credentials in full"
        )
    behind = iter(remaining)
    out = []
    for i, item in enumerate(value):
        if not _carries_mask(item):
            out.append(item)
            continue
        entry, view = next(behind)
        if not _same_but_masked(item, entry, view):
            raise CredentialReentryError(
                f"{where}[{i}] is masked but no longer matches the stored entry in its "
                "place, and nothing names it: write its credentials in full"
            )
        out.append(restore_masked(item, entry, path=f"{where}[{i}]", shown=view))
    return out


def _shown_child(shown: Any, key: Any) -> Any:
    """What a read showed under ``key`` of a mapping it showed as ``shown``."""
    if isinstance(shown, _Missing):
        return MISSING
    if isinstance(shown, dict):
        return shown.get(key, MISSING)
    return REDACTED


def _holds_mask(value: Any) -> bool:
    """Whether ``value`` is itself a masked credential, or a list of values holding one."""
    if isinstance(value, str):
        return REDACTED in value
    return isinstance(value, list) and any(isinstance(i, str) and REDACTED in i for i in value)


def _wholly_masked(shown: Any) -> bool:
    """Whether a read showed this value as a credential and nothing else."""
    if isinstance(shown, str):
        return shown == REDACTED
    return isinstance(shown, list) and bool(shown) and all(_wholly_masked(i) for i in shown)


def _field_unchanged(written: Any, stored: Any, shown: Any) -> bool:
    """Whether one field beside a masked credential leaves it where it was set.

    A masked value, or a list of them, is judged by its own restore, and a value the
    read showed only as a credential may be typed again. A URL whose password alone
    was masked may take a new password and nothing else. A mapping, or a list as
    long as the stored one, is judged item by item, so a setting inside a neighbour
    that also holds a credential still counts. Anything else is exactly as stored.
    """
    if _holds_mask(written) or _wholly_masked(shown):
        return True
    if isinstance(shown, str) and REDACTED in shown:
        return isinstance(written, str) and _shown_text(written) == shown
    if isinstance(written, dict) and isinstance(stored, dict):
        seen = shown if isinstance(shown, dict) else {}
        return all(
            _field_unchanged(
                written.get(key, MISSING), stored.get(key, MISSING), seen.get(key, MISSING)
            )
            for key in written.keys() | stored.keys()
        )
    if isinstance(written, list) and isinstance(stored, list) and len(written) == len(stored):
        views = shown if isinstance(shown, list) and len(shown) == len(stored) else None
        return all(
            _field_unchanged(item, stored[i], views[i] if views else MISSING)
            for i, item in enumerate(written)
        )
    return written == stored


def _require_unchanged_holder(value: dict, stored: Any, shown: Any, path: str) -> None:
    """Refuse a mapping that holds a masked credential and changed any other field.

    Raises:
        CredentialReentryError: A field beside a masked credential was changed,
            added or dropped.
    """
    kept = stored if isinstance(stored, dict) else {}
    masked = [key for key, item in value.items() if _holds_mask(item) and kept.get(key) is not None]
    if not masked:
        return
    # Only a mapping read back says which fields are credentials; nothing else does.
    seen = shown if isinstance(shown, dict) else {}
    changed = sorted(
        str(key)
        for key in value.keys() | kept.keys()
        if not _field_unchanged(
            value.get(key, MISSING), kept.get(key, MISSING), seen.get(key, MISSING)
        )
    )
    if changed:
        where = path or "the value"
        raise CredentialReentryError(
            f"{where} changed {', '.join(changed)} beside its masked "
            f"{', '.join(sorted(str(key) for key in masked))}, which would send the stored "
            "credential somewhere it was not set for: write the credential itself"
        )


def restore_masked(
    value: Any, stored: Any = MISSING, *, path: str = "", shown: Any = MISSING
) -> Any:
    """``value`` with every :data:`REDACTED` it carries put back to what is stored there.

    A client that reads a masked document and writes it back hands in the mask
    where the credentials were, and writing that literally would replace each
    credential with the placeholder. A list is matched as :func:`_restore_list`
    says, which refuses rather than guess which stored entry a mask stands for.
    A URL whose password alone was masked restores while the rest of it is unchanged.

    A mapping holding a masked credential restores only while every other field in
    it is as stored: a writer who cannot read a token must not be able to point the
    entry holding it at another host and have the token sent there.

    ``shown`` is ``stored`` as the read showed it, where the caller has it.

    Raises:
        MaskedValueError: The placeholder stands where nothing is stored, in a URL
            changed around its masked password, or in a list entry that cannot be
            matched to the stored one it was read from.
        CredentialReentryError: A field beside a masked credential changed.
    """
    if isinstance(value, str) and value == REDACTED:
        if isinstance(stored, _Missing) or stored is None:
            raise MaskedValueError(
                f"{path or 'the value'} carries the redaction placeholder {REDACTED!r} "
                "where nothing is stored: write the credential itself"
            )
        return stored
    if isinstance(value, str) and REDACTED in value:
        if isinstance(stored, str) and _shown_text(stored) == value:
            return stored
        raise MaskedValueError(
            f"{path or 'the value'} carries the redaction placeholder {REDACTED!r} inside "
            "it and is not what is stored there with its password masked: write it in full"
        )
    if isinstance(value, list):
        return _restore_list(value, stored, shown, path) if _carries_mask(value) else value
    if isinstance(value, dict):
        _require_unchanged_holder(value, stored, shown, path)
        kept_map = stored if isinstance(stored, dict) else {}
        return {
            key: restore_masked(
                item,
                kept_map.get(key, MISSING),
                path=f"{path}.{key}" if path else str(key),
                shown=_shown_child(shown, key),
            )
            for key, item in value.items()
        }
    return value


def restore_masked_at(doc: dict, path: str, value: Any) -> Any:
    """:func:`restore_masked` for a write of ``value`` at dot-path ``path`` in ``doc``."""
    stored = get_path(doc, path, MISSING)
    shown = MISSING if isinstance(stored, _Missing) else shown_var(doc, path, stored)
    return restore_masked(value, stored, path=path, shown=shown)


def redact_var(doc: dict, path: str, value: Any) -> Any:
    """One value about to be written at ``path`` in ``doc``, masked as a read of it would be.

    Judged by :func:`redact_resource` on a document holding only that value and the
    app ``doc`` names, so a write and a read of the same var cannot disagree.

    Raises:
        ContractError: The named app's mounted contract is there but unreadable.
    """
    probe: dict = {}
    service = _at(doc, DEPLOY_SERVICE_PATH)
    if not isinstance(service, _Missing):
        set_path(probe, DEPLOY_SERVICE_PATH, service)
    set_path(probe, path, value)
    return get_path(redact_resource(probe), path)


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


# --- judging a value before it is committed ---

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


def _chart_derived(service: str, overlay: dict) -> dict[str, str]:
    """The options the chart decides for an instance with this overlay, and what decides each.

    :data:`CHART_DERIVED` less every gated path whose chart value the overlay
    leaves empty, because the chart renders nothing for it then.
    """
    gates = CHART_DERIVED_WHEN.get(service, {})
    return {
        inner: supplier
        for inner, supplier in CHART_DERIVED.get(service, {}).items()
        if inner not in gates or _chart_value_set(overlay, gates[inner])
    }


def _chart_value_set(overlay: dict, path: str) -> bool:
    """Whether a chart value is set as a Helm ``with`` reads it: present and not empty."""
    value = _at(overlay, path)
    return not isinstance(value, _Missing) and bool(value)


def chart_supplier(service: str, path: str, overlay: dict | None = None) -> str | None:
    """What the deployment sets this option with, or None where it sets nothing.

    ``path`` is the overlay path, ``config.`` rooted, so a caller compares the
    request's own keys rather than re-deriving them. ``overlay`` is the
    instance's, which decides the options the chart sets only on a value of its own.
    """
    inner = path.split(".", 1)[1] if path.startswith(f"{CONFIG_ROOT}.") else path
    return _chart_derived(service, overlay or {}).get(inner)


def chart_env_names(service: str, overlay: dict | None = None) -> dict[str, str]:
    """Environment names the chart sets for this app, each with the option it decides.

    A supplier in :data:`CHART_DERIVED` is either an environment name or a chart
    helper, and only the names are keys here: ``ENV_NAME`` tells the two apart,
    so what an ``extraEnv`` key is compared against is data rather than prose.
    Where one variable decides several options the first is reported, which is
    enough to say what the operator would be shadowing. ``overlay`` is the
    instance's, as for :func:`chart_supplier`.
    """
    out: dict[str, str] = {}
    for inner, supplier in _chart_derived(service, overlay or {}).items():
        if ENV_NAME.match(supplier):
            out.setdefault(supplier, f"{CONFIG_ROOT}.{inner}")
    return out


# --- resolving one instance's values ---


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
    derived = _chart_derived(app_contract.service, overlay)
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
        secret = _wholly_secret(path, node, app_contract.schema)

        if path in derived:
            provenance = Provenance.CHART
        elif not isinstance(written, _Missing):
            provenance = Provenance.OVERLAY
        elif has_default:
            provenance = Provenance.DEFAULT
        else:
            provenance = Provenance.UNSET

        value = written if not isinstance(written, _Missing) else default
        # An option holding credential fields shows with only those fields masked.
        if not secret:
            parts = path.split(".")
            section = parts[-2] if len(parts) > 1 else ""
            default = _redact(default, node, app_contract.schema, parts[-1], section)
            value = _redact(value, node, app_contract.schema, parts[-1], section)
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

    # Read back through the same redaction as the values route: an undeclared key
    # named like a credential is still one.
    shown = _redact(block, app_contract.schema or None, app_contract.schema, CONFIG_ROOT)
    unknown = [
        UnknownEntry(path=f"{CONFIG_ROOT}.{path}", value=value)
        for path, value in flatten(shown).items()
        if not _declares(path, leaves)
    ]
    # Apart from `unknown`, which is an overlay key that has outrun its contract;
    # an extraEnv key is outside every contract on purpose.
    custom = [
        CustomEnvEntry(path=f"{ENV_ROOT}.{key}", value=value)
        for key, value in sorted(_redact_env(custom_env(overlay)).items())
    ]
    return ConfigView(
        available=app_contract.available, fields=fields, unknown=unknown, custom=custom
    )
