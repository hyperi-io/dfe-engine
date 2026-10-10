"""A name that ends in a newline is not that name.

``$`` also matches just before a trailing newline, so ``re.match(r"^a$", "a\\n")``
succeeds. A name checked that way reaches a file stem, a git path or a YAML key with
the newline still on it.
"""

import pytest

from dfe_engine.api.v1.hunts import HuntCreateRequest
from dfe_engine.api.v1.rules import RuleCreateRequest
from dfe_engine.appmgmt import contract
from dfe_engine.fieldmap.models import FieldMap
from dfe_engine.fieldmap.remap_view import (
    RemapColumn,
    RemapViewDefinition,
    build_remap_view_ddl,
)
from dfe_engine.source.models import validate_source_name


def _rule_name(name: str) -> None:
    RuleCreateRequest(name=name, user_sql="SELECT 1 FROM dfe.main WHERE a = 1")


def _hunt_name(name: str) -> None:
    HuntCreateRequest(name=name, cron="*/5 * * * *", customers=["org_a"], rules=["certutil"])


def _field_map_standard(name: str) -> None:
    FieldMap(standard=name)


def _field_map_source(name: str) -> None:
    FieldMap(standard="sigma", source=name)


def _source_name(name: str) -> None:
    validate_source_name(name)


def _remap_view_standard(name: str) -> None:
    RemapViewDefinition(standard=name, source_name="web")


def _remap_view_cast_type(name: str) -> None:
    column = RemapColumn(field="status", source_column="status_code", type=name)
    build_remap_view_ddl(RemapViewDefinition(standard="ecs", source_name="web", columns=[column]))


def _extra_env_name(name: str) -> None:
    if not contract.ENV_NAME.match(name):
        raise ValueError(f"{name!r} is not an environment name")


_FIELDS = {
    "rule name": (_rule_name, "windows_audit-1"),
    "hunt name": (_hunt_name, "windows_hunt-1"),
    "field map standard": (_field_map_standard, "sigma"),
    "field map source": (_field_map_source, "web-logs"),
    "source name": (_source_name, "web-logs"),
    "remap view standard": (_remap_view_standard, "ecs"),
    "remap view cast type": (_remap_view_cast_type, "Array(String)"),
    "extraEnv key": (_extra_env_name, "DFE_LOADER_KEY"),
}


@pytest.mark.parametrize(("check", "valid"), _FIELDS.values(), ids=_FIELDS)
def test_a_valid_name_is_accepted(check, valid: str):
    check(valid)


@pytest.mark.parametrize(("check", "valid"), _FIELDS.values(), ids=_FIELDS)
def test_the_same_name_with_a_trailing_newline_is_refused(check, valid: str):
    with pytest.raises(ValueError):
        check(f"{valid}\n")
