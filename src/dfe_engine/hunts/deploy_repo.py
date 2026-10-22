#  Project:      dfe-engine
#  File:         hunts/deploy_repo.py
#  Purpose:      Commit hunt and rule YAML into the deploy repo the runner syncs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Deploy-repo backend for the hunt and rule registries.

The k8s hunt runner reads its hunt and rule YAML off a git-sync of the deploy
repo's ``config/hunts`` and ``config/rules`` (dfe-infra#213), and it cannot mount
the engine's own config directory -- that is a ReadWriteOnce PVC the engine pod
holds. So with gitops on, those two directories are the store: every create,
update and delete is one gitcrud commit, routed by the deployment's posture like
any other governed write (direct in dev/solo, a review PR in production+team).

The stored bytes are whatever the registry hands over -- no metadata block, no
rewriting -- because ``hunt_runner/spec_loader.py`` and
``hunt_runner/rule_compiler.py`` parse these files as they stand.

With gitops off (the docker tier) no backend is built and the registries keep
their DirectoryConfigStore over the shared config volume the docker runner reads.

The field map, alert destination, meta schema and service config registries use
the same backend, under their own commit type, so every engine replica reads one
copy through its own clone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from scalo.logger import logger

from dfe_engine.gitcrud.commit_policy import CommitContext, CommitPolicyError, build_message
from dfe_engine.gitcrud.engine import ResourceNotFoundError
from dfe_engine.gitcrud.routing import (
    ReviewRequiredError,
    WriteOutcome,
    pr_branch_name,
    route_write,
)
from dfe_engine.yaml_utils import yaml_load_string

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path

    from dfe_engine.gitcrud.engine import GitCrud
    from dfe_engine.gitcrud.forge import ForgeProvider
    from dfe_engine.settings import DFESettings

# Both classes are hunt-domain artefacts, so both commit under the standard's
# `hunt` type (gitcrud/commit_policy.ALLOWED_TYPES).
_COMMIT_TYPE = "hunt"


@dataclass(frozen=True, slots=True)
class PendingDoc:
    """A doc as a review branch would leave it, and as the branch's merge base held it."""

    branch: str
    name: str
    doc: dict[str, Any]
    base: dict[str, Any]


def _mapping(raw: str | None) -> dict[str, Any]:
    """A branch file's YAML mapping, empty when absent or not a mapping the runner could load."""
    if raw is None:
        return {}
    try:
        doc = yaml_load_string(raw)
    except Exception:  # an unparsable hunt is one the runner refuses, so it holds no ids
        return {}
    return doc if isinstance(doc, dict) else {}


class DeployRepoStore:
    """One gitcrud resource class, written with the deployment's write posture."""

    def __init__(
        self,
        crud: GitCrud,
        cls_name: str,
        *,
        environment: str,
        mode: str,
        forge: ForgeProvider | None = None,
        commit_type: str = _COMMIT_TYPE,
    ) -> None:
        self._crud = crud
        self._cls_name = cls_name
        self._environment = environment
        self._mode = mode
        self._forge = forge
        self._commit_type = commit_type

    @property
    def directory(self) -> Path:
        """Working-tree path of the class's directory in the deploy repo."""
        return self._crud.repo_path / self._crud.resource_class(self._cls_name).directory

    @property
    def repo_path(self) -> Path:
        """Working-tree path of this replica's clone of the deploy repo."""
        return self._crud.repo_path

    def relative_path(self, name: str) -> str:
        """The repo-relative file one resource is stored in."""
        cls = self._crud.resource_class(self._cls_name)
        return f"{cls.directory}/{name}{cls.suffix}"

    def reading(self) -> AbstractContextManager[None]:
        """Take the remote's head and hold the tree, for a read of the clone's history."""
        return self._crud.reading()

    def names(self) -> list[str]:
        """Stored resource names (file stems), which are the runner's identities."""
        return self._crud.list(self._cls_name)

    def get(self, name: str) -> dict[str, Any] | None:
        """The stored doc, or None when the resource is absent.

        A name gitcrud refuses cannot name a stored file either, so it reads as
        absent and the caller's own 404 path handles it.
        """
        try:
            return self._crud.get(self._cls_name, name)
        except ResourceNotFoundError, CommitPolicyError:
            return None

    def pending(self, *, bound: Literal["read", "write"] = "write") -> list[PendingDoc]:
        """Each doc a review branch not yet merged changes, beside its merge-base doc.

        Args:
            bound: How the fetch waits on the forge (``GitopsRepo.review_branch_files``).

        Raises:
            GitopsRemoteError: The deploy repo's review branches could not be fetched.
        """
        cls = self._crud.resource_class(self._cls_name)
        docs = []
        for change in self._crud.repo.review_branch_files(cls.directory, bound=bound):
            if not change.name.endswith(cls.suffix):
                continue
            docs.append(
                PendingDoc(
                    branch=change.branch,
                    name=change.name.removesuffix(cls.suffix),
                    doc=_mapping(change.content),
                    base=_mapping(change.base_content),
                )
            )
        return docs

    def put(self, name: str, doc: dict[str, Any], *, actor: str) -> WriteOutcome:
        """Write one doc verbatim and commit it, routed by posture.

        The outcome says where the change actually landed: a production+team write
        sits on a review branch, so a caller that reports it as applied is telling
        the operator the runner has it when it does not.
        """
        summary = "update" if self.get(name) is not None else "create"
        message = self._message(name, summary, actor)

        def _write(branch: str):
            return self._crud.put(self._cls_name, name, doc, actor, message, branch=branch)

        return self._route(name, summary, actor, _write)

    def put_many(
        self, docs: dict[str, dict[str, Any]], *, actor: str, scope: str, summary: str
    ) -> WriteOutcome:
        """Write several docs verbatim in ONE commit, routed by posture as one change.

        A production+team posture puts the whole change on one review branch, so the
        docs land together or not at all.

        Args:
            docs: Each resource's whole doc, by name.
            actor: Who made the change.
            scope: What the commit subject names the change by, such as ``org-acme``.
            summary: The commit subject's summary.

        Returns:
            Where the change landed.
        """
        message = self._message(scope, summary, actor)
        items = [(self._cls_name, name, doc) for name, doc in docs.items()]

        def _write(branch: str):
            return self._crud.put_many(items, actor, message, branch=branch)

        return self._route(scope, summary, actor, _write)

    def delete(self, name: str, *, actor: str) -> WriteOutcome | None:
        """Remove one resource and commit it; None when it was not there."""
        if self.get(name) is None:
            return None
        message = self._message(name, "delete", actor)

        def _write(branch: str):
            return self._crud.delete(self._cls_name, name, actor, message, branch=branch)

        return self._route(name, "delete", actor, _write)

    def _message(self, name: str, summary: str, actor: str) -> str:
        return build_message(
            CommitContext(
                ctype=self._commit_type,
                scope=name,
                summary=summary,
                actor=actor,
                role=f"{self._rbac_class}:write",
            )
        )

    @property
    def _rbac_class(self) -> str:
        rc = self._crud.resource_class(self._cls_name)
        return rc.rbac_prefix or rc.name

    def _route(
        self,
        name: str,
        summary: str,
        actor: str,
        write: Callable[[str], Any],
    ) -> WriteOutcome:
        resource = f"{self._cls_name}/{name}"
        try:
            return route_write(
                gc=self._crud,
                forge=self._forge,
                environment=self._environment,
                mode=self._mode,
                rbac_class=self._rbac_class,
                resource=resource,
                actor=actor,
                title=f"{self._commit_type}({name}): {summary}",
                body=(
                    f"Governed change to {resource} by {actor}. Opened for review "
                    "because production+team may not commit straight to main."
                ),
                write=write,
            )
        except ReviewRequiredError:
            # Production+team with no forge: commit to a review branch, never main,
            # and say what has to happen for the runner to pick the change up.
            branch = pr_branch_name(self._rbac_class, resource, "")
            res = write(branch)
            logger.warning(
                "REVIEW REQUIRED: change committed to a branch; no forge to open a "
                "PR, so nothing reading main sees it until the branch is merged",
                actor=actor,
                resource=resource,
                branch=branch,
            )
            return WriteOutcome(
                changed=res.changed,
                commit_sha=res.commit_sha,
                review_required=True,
                branch=branch,
            )


def deploy_store(
    crud: GitCrud | None,
    cls_name: str,
    *,
    settings: DFESettings,
    forge: ForgeProvider | None = None,
    commit_type: str = _COMMIT_TYPE,
) -> DeployRepoStore | None:
    """The deploy-repo backend for one class, or None when gitops is off.

    Args:
        crud: The deploy repo, or None when gitops is off.
        cls_name: The gitcrud resource class.
        settings: The deployment posture each write is routed by.
        forge: Opens the review PR a production+team write needs.
        commit_type: The commit standard's type for the class's commits.
    """
    if crud is None:
        return None
    return DeployRepoStore(
        crud,
        cls_name,
        environment=settings.env,
        mode=settings.gitops.mode,
        forge=forge,
        commit_type=commit_type,
    )
