#  Project:      dfe-engine
#  File:         gitcrud/table_defaults.py
#  Purpose:      Admin-editable table defaults, stored in the deploy repo
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The table defaults an admin sets from the console, beside the TTL override.

Common-header type, common-header version and the merge engine live in
``governance/settings/defaults.yaml``. The TTL override stays in
``governance/settings/retention.yaml``, which :func:`dfe_engine.gitcrud.retention.set_stored`
already owns, so :func:`commit_patch` writes that key into the same file.

A source that leaves the field unset inherits the stored value the next time it
is deployed. Nothing here alters a live table: a patch naming ``ttl_days`` runs
retention reconcile, and pinning the defaults onto sources runs
:func:`dfe_engine.schema.retention.apply_pinned_defaults`, both from the API.

A common-header type is stored and compared as the bare profile name, so
``common-header/minimal`` and ``minimal`` are the same header.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from scalo.logger import logger

from dfe_engine.schema.engine_resolver import parse_engine
from dfe_engine.source.engine_registry import InvalidEngineError
from dfe_engine.source.models import (
    DEFAULT_HEADER_TYPE,
    DEFAULT_HEADER_VERSION,
    Source,
    SourceHeader,
    SourceSchema,
    engine_registry,
)

from .commit_policy import CommitContext, build_message
from .engine import GitCrud, ResourceNotFoundError
from .retention import CLASS, MAX_DEFAULT_TTL_DAYS
from .retention import KEY as TTL_KEY
from .retention import NAME as TTL_NAME

if TYPE_CHECKING:
    from dfe_engine.gitops.repo import PublishResult
    from dfe_engine.schema.applier import LiveTable

NAME = "defaults"
KEY_HEADER_TYPE = "common_header_type"
KEY_HEADER_VERSION = "common_header_version"
KEY_ENGINE = "default_engine"

# A patch argument that was not sent. None means clear the stored override.
UNSET: Any = object()

Origin = Literal["override", "deployment"]


@dataclass(frozen=True, slots=True)
class TableDefaults:
    """Effective header and engine, and the override each one came from."""

    header_type: str
    header_type_stored: str | None
    header_type_origin: Origin
    header_type_fallback: str
    header_version: str
    header_version_stored: str | None
    header_version_origin: Origin
    header_version_fallback: str
    engine: str
    engine_stored: str | None
    engine_origin: Origin
    engine_fallback: str


def header_profile_name(header_type: str) -> str:
    """The bare profile name *header_type* refers to: ``common-header/minimal`` is ``minimal``.

    A value that names no profile file comes back stripped, so a compare still sees it.
    """
    from dfe_engine.schema.schema_loader import SchemaLoadError, profile_file_stem

    try:
        stem = profile_file_stem(header_type)
    except SchemaLoadError:
        return header_type.strip()
    return stem or header_type.strip()


def _fallback_header_type(settings: Any) -> str:
    configured = getattr(settings.clickhouse, "default_header_type", "") or DEFAULT_HEADER_TYPE
    return header_profile_name(configured)


def _fallback_header_version(settings: Any) -> str:
    return getattr(settings.clickhouse, "default_header_version", "") or DEFAULT_HEADER_VERSION


def _fallback_engine(settings: Any) -> str:
    return getattr(settings.clickhouse, "default_engine", "") or "MergeTree"


def _read_doc(crud: GitCrud | None, name: str) -> dict[str, Any]:
    """The committed document, or ``{}`` when it is missing or unusable."""
    if crud is None:
        return {}
    try:
        doc = crud.get(CLASS, name)
    except ResourceNotFoundError:
        return {}
    except Exception as exc:
        logger.warning(
            "table defaults file is unreadable; using the deployment values",
            name=name,
            error=str(exc),
        )
        return {}
    if not isinstance(doc, dict):
        logger.warning(
            "table defaults file is not a mapping; using the deployment values",
            name=name,
        )
        return {}
    return dict(doc)


def _stored_text(doc: dict[str, Any], key: str) -> str | None:
    if key not in doc:
        return None
    value = doc[key]
    if isinstance(value, str) and value.strip():
        return value.strip()
    logger.warning(
        "table default is not a non-empty string; using the deployment value",
        key=key,
        value=repr(value),
    )
    return None


def _header_loads(profile: str, version: str) -> bool:
    from dfe_engine.schema.schema_loader import SchemaLoader, SchemaLoadError

    try:
        SchemaLoader.load_profile(profile, profile_version=version)
    except SchemaLoadError as exc:
        logger.warning(
            "stored common-header default does not load; using the deployment header",
            profile=profile,
            version=version,
            error=str(exc),
        )
        return False
    return True


def _engine_ok(engine: str) -> bool:
    try:
        engine_registry().validate_arguments(engine)
    except InvalidEngineError as exc:
        logger.warning(
            "stored engine default is not a permitted variant; using the deployment engine",
            engine=engine,
            error=str(exc),
        )
        return False
    return True


def resolve(crud: GitCrud | None, settings: Any) -> TableDefaults:
    """Stored header and engine when they are usable, else the deployment values.

    Without gitops there is nowhere to store an override, so the deployment
    values are the whole answer. A hand-edited value that cannot be used reads
    as no override.
    """
    fallback_type = _fallback_header_type(settings)
    fallback_version = _fallback_header_version(settings)
    fallback_engine = _fallback_engine(settings)
    doc = _read_doc(crud, NAME)
    header_type_stored = _stored_text(doc, KEY_HEADER_TYPE)
    if header_type_stored is not None:
        header_type_stored = header_profile_name(header_type_stored)
    header_version_stored = _stored_text(doc, KEY_HEADER_VERSION)
    engine_stored = _stored_text(doc, KEY_ENGINE)

    header_type = header_type_stored or fallback_type
    header_version = header_version_stored or fallback_version
    if (header_type_stored or header_version_stored) and not _header_loads(
        header_type, header_version
    ):
        header_type_stored = None
        header_version_stored = None
        header_type = fallback_type
        header_version = fallback_version

    if engine_stored is not None and not _engine_ok(engine_stored):
        engine_stored = None

    return TableDefaults(
        header_type=header_type,
        header_type_stored=header_type_stored,
        header_type_origin="override" if header_type_stored is not None else "deployment",
        header_type_fallback=fallback_type,
        header_version=header_version,
        header_version_stored=header_version_stored,
        header_version_origin="override" if header_version_stored is not None else "deployment",
        header_version_fallback=fallback_version,
        engine=engine_stored or fallback_engine,
        engine_stored=engine_stored,
        engine_origin="override" if engine_stored is not None else "deployment",
        engine_fallback=fallback_engine,
    )


def _clean_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _next_stored(current: str | None, change: Any, label: str) -> str | None:
    if change is UNSET:
        return current
    if change is None:
        return None
    return _clean_text(change, label)


def _apply(doc: dict[str, Any], key: str, value: str | None) -> bool:
    """Set or drop *key*. Return whether *doc* changed."""
    if value is None:
        if key not in doc:
            return False
        doc.pop(key)
        return True
    if doc.get(key) == value:
        return False
    doc[key] = value
    return True


def commit_patch(
    crud: GitCrud,
    *,
    actor: str,
    settings: Any | None = None,
    ttl_days: Any = UNSET,
    common_header_type: Any = UNSET,
    common_header_version: Any = UNSET,
    engine: Any = UNSET,
) -> PublishResult | None:
    """Commit the named overrides. ``None`` clears a key; :data:`UNSET` leaves it.

    Header type and version are checked as the pair a later deploy would load.
    The engine is checked against the variant registry. TTL uses the same bounds
    as the retention override. Nothing is committed when every named value is
    already stored that way, or when a value is refused.

    Raises:
        ValueError: A named value cannot be stored.
    """
    if ttl_days is not UNSET and ttl_days is not None:
        if isinstance(ttl_days, bool) or not isinstance(ttl_days, int):
            raise ValueError(f"ttl_days must be an int, not {ttl_days!r}")
        if not 0 <= ttl_days <= MAX_DEFAULT_TTL_DAYS:
            raise ValueError(f"ttl_days must be 0..{MAX_DEFAULT_TTL_DAYS}, not {ttl_days}")

    if engine is not UNSET and engine is not None:
        engine = _clean_text(engine, "engine")
        try:
            engine_registry().validate_arguments(engine)
        except InvalidEngineError as exc:
            raise ValueError(str(exc)) from exc

    fallback_type = DEFAULT_HEADER_TYPE if settings is None else _fallback_header_type(settings)
    fallback_version = (
        DEFAULT_HEADER_VERSION if settings is None else _fallback_header_version(settings)
    )
    current = _read_doc(crud, NAME)
    header_type = _next_stored(
        _stored_text(current, KEY_HEADER_TYPE), common_header_type, "common_header_type"
    )
    if header_type is not None:
        header_type = header_profile_name(header_type)
    header_version = _next_stored(
        _stored_text(current, KEY_HEADER_VERSION),
        common_header_version,
        "common_header_version",
    )
    if (common_header_type is not UNSET or common_header_version is not UNSET) and (
        header_type is not None or header_version is not None
    ):
        profile = header_type or fallback_type
        version = header_version or fallback_version
        from dfe_engine.schema.schema_loader import SchemaLoader, SchemaLoadError

        try:
            SchemaLoader.load_profile(profile, profile_version=version)
        except SchemaLoadError as exc:
            raise ValueError(str(exc)) from exc

    defaults_changed = False
    if common_header_type is not UNSET and _apply(current, KEY_HEADER_TYPE, header_type):
        defaults_changed = True
    if common_header_version is not UNSET and _apply(current, KEY_HEADER_VERSION, header_version):
        defaults_changed = True
    if engine is not UNSET:
        stored_engine = None if engine is None else engine
        if _apply(current, KEY_ENGINE, stored_engine):
            defaults_changed = True

    retention = _read_doc(crud, TTL_NAME)
    retention_changed = False
    if ttl_days is not UNSET:
        retention_changed = _apply(retention, TTL_KEY, ttl_days)

    if not defaults_changed and not retention_changed:
        return None

    if (
        ttl_days is not UNSET
        and common_header_type is UNSET
        and common_header_version is UNSET
        and engine is UNSET
    ):
        summary = (
            "clear the default ttl override" if ttl_days is None else f"default ttl {ttl_days} days"
        )
    else:
        summary = "update table defaults"
    message = build_message(CommitContext(ctype="cfg", scope=NAME, summary=summary, actor=actor))
    items: list[tuple[str, str, dict[str, Any]]] = []
    if defaults_changed:
        items.append((CLASS, NAME, current))
    if retention_changed:
        items.append((CLASS, TTL_NAME, retention))
    return crud.put_many(items, actor, message)


def pin_table_defaults(
    source: Source,
    *,
    header_type: str,
    header_version: str,
    ttl_days: int,
    engine: str,
) -> Source | None:
    """Pin the current version to these defaults. None when that version already has them.

    Other versions are left alone, and so is every other field on the current
    version. Header, TTL and engine are written even when the source was only
    inheriting the same values, so a later change to the deployment default
    does not move this source again. The header type is written as the bare
    profile name, and a stored ``common-header/<name>`` already matches ``<name>``.
    """
    snap = source.versions[source.current]
    header = SourceHeader(type=header_profile_name(header_type), version=header_version)
    schema = (
        snap.schema_config.model_copy(update={"ttl_days": ttl_days, "engine": engine})
        if snap.schema_config is not None
        else SourceSchema(ttl_days=ttl_days, engine=engine)
    )
    if (
        snap.header is not None
        and header_profile_name(snap.header.type) == header.type
        and snap.header.version == header.version
        and snap.schema_config is not None
        and snap.schema_config.ttl_days == ttl_days
        and snap.schema_config.engine == engine
    ):
        return None
    versions = dict(source.versions)
    versions[source.current] = snap.model_copy(update={"header": header, "schema_config": schema})
    return source.model_copy(update={"versions": versions})


@dataclass(frozen=True)
class StoredDefault:
    """One field as the source stores it and as its table runs it, beside the default.

    ``stored`` is None when the source leaves the field unset and therefore
    inherits. ``live`` is the deployed table's value, None when no table was
    read or the field is not a table property. ``drifted`` is decided by
    :func:`source_default_drift`.
    """

    stored: str | int | None
    default: str | int | None
    live: str | int | None = None
    drifted: bool = False


def _stored_differs(stored: str | int | None, default: str | int | None) -> bool:
    return stored is not None and stored != default


def _live_ttl_value(live: LiveTable) -> int | str:
    """Days for a table with no TTL (0) or a whole-day one; else the interval, such as ``6 hour``."""
    if live.ttl is None:
        return 0
    days = live.ttl.days
    return live.ttl.describe() if days is None else days


@dataclass(frozen=True)
class SourceDrift:
    """How one source's current version compares with the table defaults."""

    source: str
    core: bool
    ttl_days: StoredDefault
    common_header_type: StoredDefault
    common_header_version: StoredDefault
    engine: StoredDefault

    @property
    def drifted(self) -> tuple[str, ...]:
        """The fields that differ from the defaults, in report order."""
        return tuple(name for name in _DRIFT_FIELDS if getattr(self, name).drifted)


_DRIFT_FIELDS = ("ttl_days", "common_header_type", "common_header_version", "engine")


def source_default_drift(
    source: Source,
    *,
    header_type: str,
    header_version: str,
    ttl_days: int,
    engine: str,
    live: LiveTable | None = None,
) -> SourceDrift | None:
    """The current version's drift from these defaults, or None when it has none.

    TTL and engine belong to the table. With *live*, the source's deployed
    table, they are measured on it, so a value the table never took is drift
    whatever the source stores; a table without a TTL runs 0 days, a TTL that is
    not whole days is reported as its interval and is always drift, and the
    engine compares as a variant without its topology prefix. Without *live*,
    and always for the header, the stored value is measured: unset inherits on
    the next deploy and is not drift, nor is a stored value equal to the
    default. Header types compare as bare profile names.
    """
    snap = source.versions[source.current]
    header = snap.header
    schema = snap.schema_config
    stored_ttl = None if schema is None else schema.ttl_days
    stored_engine = None if schema is None or not schema.engine else schema.engine
    stored_type = None if header is None else header.type
    stored_version = None if header is None else header.version
    if live is None:
        ttl_field = StoredDefault(
            stored=stored_ttl, default=ttl_days, drifted=_stored_differs(stored_ttl, ttl_days)
        )
        engine_field = StoredDefault(
            stored=stored_engine, default=engine, drifted=_stored_differs(stored_engine, engine)
        )
    else:
        live_ttl = _live_ttl_value(live)
        ttl_field = StoredDefault(
            stored=stored_ttl, default=ttl_days, live=live_ttl, drifted=live_ttl != ttl_days
        )
        engine_field = StoredDefault(
            stored=stored_engine,
            default=engine,
            live=live.variant,
            drifted=live.variant != parse_engine(engine).variant,
        )
    report = SourceDrift(
        source=source.source,
        core=source.resource_type == "core",
        ttl_days=ttl_field,
        common_header_type=StoredDefault(
            stored=stored_type,
            default=header_type,
            drifted=stored_type is not None
            and header_profile_name(stored_type) != header_profile_name(header_type),
        ),
        common_header_version=StoredDefault(
            stored=stored_version,
            default=header_version,
            drifted=_stored_differs(stored_version, header_version),
        ),
        engine=engine_field,
    )
    if not report.drifted:
        return None
    return report
