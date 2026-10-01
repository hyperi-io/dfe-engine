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
is deployed. Nothing here alters a live table: retention reconcile is the only
apply, and it runs from the API when the patch names ``ttl_days``.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from scalo.logger import logger

from dfe_engine.source.engine_registry import InvalidEngineError
from dfe_engine.source.models import DEFAULT_HEADER_TYPE, DEFAULT_HEADER_VERSION, engine_registry

from .commit_policy import CommitContext, build_message
from .engine import GitCrud, ResourceNotFoundError
from .retention import CLASS, MAX_DEFAULT_TTL_DAYS
from .retention import KEY as TTL_KEY
from .retention import NAME as TTL_NAME

if TYPE_CHECKING:
    from dfe_engine.gitops.repo import PublishResult

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


def _fallback_header_type(settings: Any) -> str:
    return getattr(settings.clickhouse, "default_header_type", "") or DEFAULT_HEADER_TYPE


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
