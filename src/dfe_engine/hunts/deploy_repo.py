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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scalo.logger import logger

from dfe_engine.gitcrud.commit_policy import CommitContext, CommitPolicyError, build_message
from dfe_engine.gitcrud.engine import ResourceNotFoundError
from dfe_engine.gitcrud.routing import (
    ReviewRequiredError,
    WriteOutcome,
    pr_branch_name,
    route_write,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from dfe_engine.gitcrud.engine import GitCrud
    from dfe_engine.gitcrud.forge import ForgeProvider
    from dfe_engine.settings import DFESettings

# Both classes are hunt-domain artefacts, so both commit under the standard's
# `hunt` type (gitcrud/commit_policy.ALLOWED_TYPES).
_COMMIT_TYPE = "hunt"


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
    ) -> None:
        self._crud = crud
        self._cls_name = cls_name
        self._environment = environment
        self._mode = mode
        self._forge = forge

    @property
    def directory(self) -> Path:
        """Working-tree path of the class's directory in the deploy repo."""
        return self._crud.repo_path / self._crud.resource_class(self._cls_name).directory

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
                ctype=_COMMIT_TYPE,
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
                title=f"{_COMMIT_TYPE}({name}): {summary}",
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
                "PR, so the hunt runner sees it only once the branch is merged",
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
) -> DeployRepoStore | None:
    """The deploy-repo backend for one hunt-domain class; None when gitops is off."""
    if crud is None:
        return None
    return DeployRepoStore(
        crud,
        cls_name,
        environment=settings.env,
        mode=settings.gitops.mode,
        forge=forge,
    )
