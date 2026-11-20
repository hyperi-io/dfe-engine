#  Project:      dfe-engine
#  File:         orgs/home.py
#  Purpose:      Which registered org is the deployment's own
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Which registered org is the deployment's own ("your org"), the one root accounts are bound to."""

from pathlib import Path
from typing import TYPE_CHECKING

from dfe_engine.gitcrud.commit_policy import CommitContext, build_message
from dfe_engine.gitcrud.engine import ResourceNotFoundError
from dfe_engine.yaml_utils import yaml_dump, yaml_load

if TYPE_CHECKING:
    from dfe_engine.gitcrud.engine import GitCrud

CLASS = "gov_settings"
KEY = "name"
NAME = "organisation"
# With gitops off the doc keeps the deploy repo's path, under the config directory.
PLAIN_FILE = Path("governance") / "settings" / f"{NAME}.yaml"


class HomeOrgStore:
    """Your org's name, in the deploy repo's ``governance/settings/organisation.yaml``, else that path under the config directory."""

    def __init__(self, *, config_dir: Path, crud: GitCrud | None) -> None:
        self._crud = crud
        self._known: str | None = None
        self._path = config_dir / PLAIN_FILE

    def _doc(self) -> object:
        """The stored doc, an empty mapping when there is none."""
        if self._crud is not None:
            try:
                return self._crud.get(cls_name=CLASS, name=NAME)
            except ResourceNotFoundError:
                return {}
        if not (self._path.is_file()):
            return {}
        return yaml_load(source=self._path) or {}

    def get(self) -> str | None:
        """Your org's name, None when none is set. Raises ``ValueError`` when the doc holds anything but a name."""
        doc = self._doc()
        if not (isinstance(doc, dict)):
            raise ValueError(f"The your-organisation doc is {doc!r}, not a mapping")
        value = doc.get(KEY)
        if value is None:
            self._known = None
            return None
        if (not (isinstance(value, str))) or (not (value)):
            raise ValueError(f"The your-organisation doc names {value!r}, not an org")
        self._known = value
        return value

    @property
    def known(self) -> str | None:
        """Your org's name as this replica last read or wrote it, without reading the doc again."""
        return self._known

    def set(self, *, actor: str, name: str) -> None:
        """Name *name* as your org: a direct commit to the deploy repo, else the plain file."""
        if self._crud is None:
            self._path.parent.mkdir(exist_ok=True, parents=True)
            yaml_dump(data={KEY: name}, dest=self._path)
            self._known = name
            return
        context = CommitContext(
            actor=actor, ctype="cfg", scope=NAME, summary=f"your organisation {name}"
        )
        self._crud.set_key(
            actor=actor,
            cls_name=CLASS,
            dotpath=KEY,
            message=build_message(context),
            name=NAME,
            value=name,
        )
        self._known = name
