#  Project:      dfe-engine
#  File:         cli/auto/local.py
#  Purpose:      `dfe local` - break-glass direct gitops CRUD when the daemon is dead
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``dfe local`` - the break-glass command group for a DEAD ``dfe-engine`` daemon.

When the engine (and therefore the RBAC'd API window over gitops) is down, an
operator still needs to change the customer-owned deploy repo - the headline case
being a rogue pod: scale a helm var to 0, fix a KEDA dial. This group writes
DIRECTLY to the local gitops clone using the SAME GitCrud write-engine the API
uses (a second thin adapter over the one engine, never a reimplementation), so
every write lands as one YAML-in-git commit exactly like the daemon would produce.

Two things make it a BREAK-GLASS surface, not an RBAC bypass in disguise:

- There is no RBAC here (the daemon that enforces it is dead); write authority IS
  git access to the clone. Every write shows a target header + a diff + a default
  y/N confirm first.
- Every write is marked unmistakably: subject prefixed ``[BREAK-GLASS] `` and the
  trailers ``DFE-Break-Glass: true`` / ``DFE-Actor:`` / ``DFE-Reason:`` (reason is
  REQUIRED). ``dfe local log`` flags these entries so the audit trail shows plainly
  that the change went round the daemon.

GitCrud is built standalone (no ``app.state`` - the daemon is dead) from
``settings.gitops`` via the same construction the daemon uses, but with
``push=False`` so a break-glass write only commits LOCALLY; pushing is a separate
deliberate step (``dfe local push``, or the post-write prompt). This matches the
gitops-survivability model: the mutation is a git commit, publishing it is its own
act.
"""

import copy
import difflib
import functools
import getpass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from dfe_engine.gitcrud import (
    GitCrud,
    ResourceNotFoundError,
    UnknownResourceClassError,
    VersionConflictError,
    VersionedDoc,
    get_path,
    set_path,
)
from dfe_engine.gitcrud.commit_policy import (
    CommitContext,
    CommitPolicyError,
    build_message,
    type_for_class,
    validate_change,
)
from dfe_engine.gitcrud.log import read_log
from dfe_engine.gitops.dulwich_auth import authed_https_url
from dfe_engine.gitops.repo import GitopsDivergedError, GitopsRemoteError, GitopsRepo
from dfe_engine.settings import get_settings
from dfe_engine.yaml_utils import yaml_dump_string

BREAK_GLASS_PREFIX = "[BREAK-GLASS] "
_MISSING = object()


# --- construction (standalone GitCrud over the local clone) ------------------


def _gitops():
    """Resolved gitops settings (local_path / repo_url / branch / push auth)."""
    return get_settings().gitops


def _resolve_repo_path(repo: str | None) -> str:
    """The clone path to operate on: ``--repo`` override, else configured local_path."""
    local = repo or _gitops().local_path
    if not local:
        raise click.ClickException(
            "no gitops clone path: pass --repo <path> or set DFE_GITOPS_LOCAL_PATH."
        )
    return local


def _build_crud(repo: str | None) -> GitCrud:
    """Build GitCrud the SAME way the daemon does, but standalone + push-disabled.

    The daemon builds GitCrud from ``settings.gitops`` via gitcrud.factory; we mirror
    that construction here (no ``app.state`` - the daemon is dead) pointed at the
    local clone. ``push=False`` is deliberate: a break-glass write commits LOCALLY
    only, and publishing is a separate step (``dfe local push`` / the post-write
    prompt) so the operator never fires an unreviewed change straight at the remote.
    """
    gs = _gitops()
    grepo = GitopsRepo(
        local_path=_resolve_repo_path(repo),
        repo_url=gs.repo_url,
        branch=gs.branch,
        push=False,
        username=gs.username,
        token=gs.token,
        author_name=gs.author_name,
        author_email=gs.author_email,
    )
    return GitCrud(grepo)


# --- break-glass commit marking ---------------------------------------------


def _oneline(value: object) -> str:
    """Collapse newlines so an operator-supplied reason/actor cannot forge trailers."""
    return " ".join(str(value).splitlines()).strip()


def _break_glass_message(ctype: str, scope: str, summary: str, actor: str, reason: str) -> str:
    """Compose a break-glass commit message: distinct subject + audit trailers.

    Reuses ``commit_policy.build_message`` to render + ASCII/budget-validate the
    conforming ``type(scope): summary`` subject, then WRAPS it: the literal
    ``[BREAK-GLASS] `` prefix (so it never reads as an ordinary ``cfg(...)`` commit
    and read_log's subject regex deliberately does not match it) plus the
    ``DFE-Break-Glass``/``DFE-Actor``/``DFE-Reason`` trailers. We cannot fold the
    prefix into build_message itself (validate_subject would reject the 14-char
    prefix + over-50 length), and commit_policy is owned elsewhere - so this is the
    thin break-glass wrapper the design calls for.
    """
    base = build_message(
        CommitContext(ctype=ctype, scope=_oneline(scope), summary=_oneline(summary), actor=actor)
    )
    subject = base.splitlines()[0]  # conforming "type(scope): summary", validated
    trailers = [
        "DFE-Break-Glass: true",
        f"DFE-Actor: {_oneline(actor)}",
        f"DFE-Reason: {_oneline(reason)}",
    ]
    return f"{BREAK_GLASS_PREFIX}{subject}\n\n" + "\n".join(trailers) + "\n\n[skip ci]"


def _bg_message_for_class(
    crud: GitCrud, cls: str, name: str, summary: str, actor: str, reason: str
) -> str:
    """Break-glass message with the commit TYPE resolved from the resource class."""
    rc = crud.resource_class(cls)
    return _break_glass_message(
        type_for_class(rc.rbac_prefix or rc.name), name, summary, actor, reason
    )


# --- guard / reason / diff / push UX ----------------------------------------


def _resolve_actor(actor: str | None) -> str:
    return actor or getpass.getuser()


def _resolve_reason(reason: str | None, *, allow_prompt: bool) -> str:
    """Return a non-empty break-glass reason, prompting when allowed.

    ``--reason`` is REQUIRED. Interactively (no ``--yes``) an omitted reason is
    prompted for; with ``--yes`` (CI / non-interactive) an omitted reason is a hard
    error rather than a silent unattributed write.
    """
    if reason and reason.strip():
        return reason.strip()
    if allow_prompt:
        try:
            entered = click.prompt("Break-glass reason (required)", default="", show_default=False)
        except click.Abort:
            entered = ""
        if entered and entered.strip():
            return entered.strip()
    raise click.ClickException("a break-glass reason is required: pass --reason '<why>'.")


def _guard(*, repo_path: Path, diff_text: str, yes: bool) -> None:
    """Print the target header + diff, then a default y/N confirm (skipped by --yes)."""
    gs = _gitops()
    click.echo(f"writing to {repo_path} (remote {gs.repo_url or '(none)'}, branch {gs.branch})")
    click.echo(diff_text)
    if yes:
        return
    if not click.confirm("Proceed with this break-glass write?", default=False):
        raise click.Abort()


def _safety_warn(path: str, value: object, *, yes: bool) -> None:
    """Reuse the API's value-safety validator (``validate_change``) as an
    OVERRIDABLE warning, NOT a block.

    Break-glass can override anything - RBAC is dead and git access is the
    authority - so this is not the API's hard 403. But it runs the SAME
    value-safety check the daemon's helm endpoint runs, so a stressed operator
    who does not know the helm complexity gets the steer the API would give
    (e.g. an unpinned/floating image ref, or a KEDA-managed key) BEFORE they
    commit a change that would not work or would re-break the daemon. This is
    the safety half of the API guard; the RBAC half (protected-var needs the
    ``helmvars:override`` grant) is deliberately dropped. ``--yes`` proceeds
    without asking.
    """
    try:
        validate_change(path, value)
    except CommitPolicyError as exc:
        click.echo(f"safety: {exc}", err=True)
        click.echo("  (the daemon's API would reject this; break-glass can override it)", err=True)
        if not yes and not click.confirm("Override this safety guard?", default=False):
            raise click.Abort() from exc


def _yaml_diff(before: dict | None, after: dict | None) -> str:
    """Unified diff of two YAML docs (empty doc -> no lines)."""
    b = yaml_dump_string(before).splitlines(keepends=True) if before else []
    a = yaml_dump_string(after).splitlines(keepends=True) if after else []
    diff = "".join(difflib.unified_diff(b, a, fromfile="before", tofile="after"))
    return diff or "(no textual change)"


def _do_push(repo_path: str) -> None:
    """Push the local clone's committed break-glass commits to the configured remote.

    Builds a push-enabled GitopsRepo over the SAME clone, so the auth URL, the write
    timeout and the retry budget are the daemon's, and pushes the local commits as a
    fast-forward. A remote that has moved on is refused with the commits kept, never
    reset away. No remote configured => a clean no-op.
    """
    gs = _gitops()
    if not gs.repo_url:
        click.echo("no remote configured (DFE_GITOPS_REPO_URL unset); nothing pushed.")
        return
    repo = GitopsRepo(
        local_path=repo_path,
        repo_url=gs.repo_url,
        branch=gs.branch,
        push=True,
        username=gs.username,
        token=gs.token,
        author_name=gs.author_name,
        author_email=gs.author_email,
        write=gs.write,
    )
    repo.ensure()
    try:
        result = repo.push_local_commits()
    except GitopsDivergedError as exc:
        raise click.ClickException(
            f"{exc}. Nothing was pushed and the local commits are kept: bring {gs.branch} "
            "from the remote into this clone with git, then run `dfe local push` again."
        ) from exc
    except GitopsRemoteError as exc:
        raise click.ClickException(f"push to {gs.repo_url} failed: {exc}") from exc
    if result.pushed:
        click.echo(f"pushed to {gs.repo_url} ({gs.branch}).")
    else:
        click.echo("nothing to push (local branch not ahead of remote).")


def _maybe_push(repo_path: Path, *, push: bool | None, yes: bool) -> None:
    """Handle the post-write push: --push pushes, --no-push skips, else prompt.

    ``--yes`` (CI) suppresses the prompt and, absent an explicit ``--push``, leaves
    the commit local (safe default - publishing stays a deliberate act).
    """
    if push is False:
        click.echo("committed locally; not pushed (--no-push). Run `dfe local push` to publish.")
        return
    do = push is True
    if push is None and not yes:
        do = click.confirm("Push to the remote now?", default=False)
    if not do:
        click.echo("committed locally; not pushed. Run `dfe local push` to publish.")
        return
    _do_push(str(repo_path))


# --- shared option decorators ------------------------------------------------


def _repo_option(f):
    return click.option(
        "--repo",
        default=None,
        metavar="PATH",
        help="Gitops clone path (default: configured DFE_GITOPS_LOCAL_PATH).",
    )(f)


def _write_options(f):
    """The five options every break-glass WRITE shares."""
    f = _repo_option(f)
    f = click.option(
        "--actor", default=None, help="Operator identity for the audit trailer (default: OS user)."
    )(f)
    f = click.option(
        "--reason", default=None, help="REQUIRED break-glass reason (prompted if omitted)."
    )(f)
    f = click.option(
        "--yes", "-y", is_flag=True, help="Skip the confirm prompt (CI). Requires --reason."
    )(f)
    f = click.option(
        "--push/--no-push",
        "push",
        default=None,
        help="Push after the local commit (default: prompt).",
    )(f)
    return f


def _friendly(fn):
    """Turn the engine's typed errors into clean CLI failures (no tracebacks)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        try:
            return fn(*args, **kwargs)
        except (
            UnknownResourceClassError,
            ResourceNotFoundError,
            VersionConflictError,
            ValueError,
        ) as exc:
            raise click.ClickException(str(exc)) from exc

    return wrapper


def _parse_value(value: str) -> object:
    """Parse a CLI value as JSON (numbers/bools/null/objects), else the raw string."""
    import json

    try:
        return json.loads(value)
    except ValueError, TypeError:
        return value


# --- the command group -------------------------------------------------------


@click.group(name="local", no_args_is_help=True)
def local_group() -> None:
    """Break-glass direct gitops CRUD for when the dfe-engine daemon is DEAD.

    Writes DIRECTLY to the local deploy-repo clone via the same GitCrud engine the
    API uses; every write is marked [BREAK-GLASS] and needs a --reason. Headline
    case - a rogue pod:

        dfe local set helmvars receiver-default keda.maxReplicas 0 --reason "rogue pod"

    Read first (classes/ls/get/log/versions/status), fix (set/unset/rm/revert/
    restore), then publish (push). Authority here is git access to the clone, not
    RBAC - the daemon that enforces RBAC is down.
    """


@local_group.command("classes")
@_repo_option
@_friendly
def classes_cmd(repo: str | None) -> None:
    """List the resource classes GitCrud can address (helmvars, governance, ...)."""
    crud = _build_crud(repo)
    for c in crud.registry.all():
        tag = " (versioned)" if c.versioned else ""
        click.echo(f"{c.name:18} {c.directory}{tag}")


@local_group.command("ls")
@click.argument("cls")
@_repo_option
@_friendly
def ls_cmd(cls: str, repo: str | None) -> None:
    """List resources in a class, e.g. `dfe local ls helmvars`."""
    names = _build_crud(repo).list(cls)
    if not names:
        click.echo("(none)")
        return
    for n in names:
        click.echo(n)


@local_group.command("grep")
@click.argument("term")
@_repo_option
@_friendly
def grep_cmd(term: str, repo: str | None) -> None:
    """Search the gitops repo's YAML for TERM, e.g. find every keda.maxReplicas dial."""
    root = Path(_resolve_repo_path(repo))
    hits = 0
    for pattern in ("*.yaml", "*.yml"):
        for f in sorted(root.rglob(pattern)):
            if ".git" in f.parts or not f.is_file():
                continue
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), start=1):
                if term in line:
                    click.echo(f"{f.relative_to(root)}:{i}: {line.strip()}")
                    hits += 1
    if not hits:
        click.echo(f"no match for {term!r}")


@local_group.command("get")
@click.argument("cls")
@click.argument("name")
@click.option("--path", default=None, help="Dot-path of a single key, e.g. keda.maxReplicas.")
@_repo_option
@_friendly
def get_cmd(cls: str, name: str, path: str | None, repo: str | None) -> None:
    """Show a resource doc, or one key with --path (e.g. keda.maxReplicas)."""
    doc = _build_crud(repo).get(cls, name)
    if path is None:
        click.echo(yaml_dump_string(doc), nl=False)
        return
    value = get_path(doc, path, _MISSING)
    if value is _MISSING:
        raise click.ClickException(f"no such path {path!r} in {cls}/{name}")
    if isinstance(value, (dict, list)):
        click.echo(yaml_dump_string(value), nl=False)
    else:
        click.echo(str(value))


@local_group.command("log")
@click.option("--limit", default=50, help="Max entries to show (newest first).")
@_repo_option
@_friendly
def log_cmd(limit: int, repo: str | None) -> None:
    """Show the gitcrud audit log, flagging break-glass commits with !! BREAK-GLASS."""
    entries, _ = read_log(_build_crud(repo), limit=limit)
    if not entries:
        click.echo("no gitcrud history yet.")
        return
    for e in entries:
        # A break-glass commit is exactly one carrying DFE-Break-Glass: true; read_log
        # surfaces the [BREAK-GLASS] subject prefix as the (non-conforming) summary,
        # which is written atomically with that trailer - so the prefix flags them.
        flag = "!! BREAK-GLASS " if e.summary.startswith(BREAK_GLASS_PREFIX) else ""
        when = datetime.fromtimestamp(e.timestamp, tz=UTC).strftime("%Y-%m-%d %H:%M")
        click.echo(f"{e.sha[:7]} {when} {e.actor:14} {flag}{e.summary}")


@local_group.command("versions")
@click.argument("cls")
@click.argument("name")
@_repo_option
@_friendly
def versions_cmd(cls: str, name: str, repo: str | None) -> None:
    """List published versions of a versioned resource (e.g. ch_tiers)."""
    versions = VersionedDoc(_build_crud(repo)).list_versions(cls, name)
    if not versions:
        click.echo("(no published versions)")
        return
    for v in versions:
        click.echo(str(v))


@local_group.command("status")
@_repo_option
@_friendly
def status_cmd(repo: str | None) -> None:
    """Show working-tree changes + unpushed-commit count for the clone."""
    from dulwich import porcelain

    repo_path = _resolve_repo_path(repo)
    gs = _gitops()
    crud = _build_crud(repo)
    click.echo(f"repo: {repo_path}")
    click.echo(f"branch: {gs.branch}  head: {crud.head_revision() or '(none)'}")

    st = porcelain.status(repo_path)
    staged = [*st.staged["add"], *st.staged["modify"], *st.staged["delete"]]
    dirty = staged or st.unstaged or st.untracked
    if not dirty:
        click.echo("working tree: clean")
    else:
        click.echo(
            f"working tree: {len(staged)} staged, "
            f"{len(st.unstaged)} modified, {len(st.untracked)} untracked"
        )

    ahead = _count_unpushed(repo_path, gs)
    if ahead is None:
        click.echo("unpushed: unknown (no remote or remote unreachable)")
    else:
        click.echo(f"unpushed commits: {ahead}")


def _count_unpushed(repo_path: str, gs) -> int | None:
    """Best-effort count of local commits ahead of the remote branch (None = unknown)."""
    if not gs.repo_url:
        return None
    try:
        from dulwich import porcelain
        from dulwich.repo import Repo

        result = porcelain.ls_remote(authed_https_url(gs.repo_url, gs.username, gs.token))
        refs = getattr(result, "refs", result) or {}
        remote_sha = refs.get(f"refs/heads/{gs.branch}".encode())
        if remote_sha is None:
            return None
        with Repo(str(repo_path)) as r:
            local = r.head()
            if remote_sha == local:
                return 0
            reachable = {e.commit.id for e in r.get_walker(include=[remote_sha])}
            count = 0
            for entry in r.get_walker(include=[local]):
                if entry.commit.id in reachable:
                    break
                count += 1
            return count
    except Exception:
        return None


@local_group.command("set")
@click.argument("cls")
@click.argument("name")
@click.argument("path")
@click.argument("value")
@_write_options
@_friendly
def set_cmd(
    cls: str,
    name: str,
    path: str,
    value: str,
    repo: str | None,
    actor: str | None,
    reason: str | None,
    yes: bool,
    push: bool | None,
) -> None:
    """Set a dot-path via GitCrud.set_key, e.g. scale a rogue pod's keda dial to 0:

    dfe local set helmvars receiver-default keda.maxReplicas 0 --reason "rogue pod"
    """
    crud = _build_crud(repo)
    actor = _resolve_actor(actor)
    val = _parse_value(value)
    _safety_warn(path, val, yes=yes)
    try:
        before = crud.get(cls, name)
    except ResourceNotFoundError:
        before = {}
    after = copy.deepcopy(before)
    set_path(after, path, val)
    reason = _resolve_reason(reason, allow_prompt=not yes)
    _guard(repo_path=crud.repo_path, diff_text=_yaml_diff(before, after), yes=yes)
    msg = _bg_message_for_class(crud, cls, name, f"set {path}", actor, reason)
    result = crud.set_key(cls, name, path, val, actor, message=msg)
    click.echo(f"committed {result.commit_sha or '(unchanged)'}")
    _maybe_push(crud.repo_path, push=push, yes=yes)


@local_group.command("unset")
@click.argument("cls")
@click.argument("name")
@click.argument("path")
@_write_options
@_friendly
def unset_cmd(
    cls: str,
    name: str,
    path: str,
    repo: str | None,
    actor: str | None,
    reason: str | None,
    yes: bool,
    push: bool | None,
) -> None:
    """Remove a dot-path via GitCrud.delete_key (revert a helm var to its default)."""
    from dfe_engine.gitcrud.engine import _del_path

    crud = _build_crud(repo)
    actor = _resolve_actor(actor)
    before = crud.get(cls, name)
    after = copy.deepcopy(before)
    _del_path(after, path)
    reason = _resolve_reason(reason, allow_prompt=not yes)
    _guard(repo_path=crud.repo_path, diff_text=_yaml_diff(before, after), yes=yes)
    msg = _bg_message_for_class(crud, cls, name, f"unset {path}", actor, reason)
    result = crud.delete_key(cls, name, path, actor, message=msg)
    click.echo(f"committed {result.commit_sha or '(unchanged)'}")
    _maybe_push(crud.repo_path, push=push, yes=yes)


@local_group.command("rm")
@click.argument("cls")
@click.argument("name")
@_write_options
@_friendly
def rm_cmd(
    cls: str,
    name: str,
    repo: str | None,
    actor: str | None,
    reason: str | None,
    yes: bool,
    push: bool | None,
) -> None:
    """Delete a whole resource via GitCrud.delete (e.g. remove a helm overlay)."""
    crud = _build_crud(repo)
    actor = _resolve_actor(actor)
    before = crud.get(cls, name)  # ResourceNotFoundError if absent
    reason = _resolve_reason(reason, allow_prompt=not yes)
    _guard(repo_path=crud.repo_path, diff_text=_yaml_diff(before, {}), yes=yes)
    msg = _bg_message_for_class(crud, cls, name, "delete", actor, reason)
    result = crud.delete(cls, name, actor, message=msg)
    click.echo(f"committed {result.commit_sha or '(unchanged)'}")
    _maybe_push(crud.repo_path, push=push, yes=yes)


@local_group.command("revert")
@click.argument("sha")
@_write_options
@_friendly
def revert_cmd(
    sha: str,
    repo: str | None,
    actor: str | None,
    reason: str | None,
    yes: bool,
    push: bool | None,
) -> None:
    """Create a revert-commit that undoes an earlier commit's file changes.

    The one genuinely-new op (GitCrud has no revert): compute the inverse of the
    target commit's tree diff and commit it back through GitopsRepo (dulwich, the
    same publish path), marked break-glass.
    """
    from dulwich.diff_tree import tree_changes
    from dulwich.repo import Repo

    crud = _build_crud(repo)
    actor = _resolve_actor(actor)
    repo_path = crud.repo_path

    with Repo(str(repo_path)) as r:
        try:
            commit = r[sha.encode()]
        except KeyError as exc:
            raise click.ClickException(f"unknown commit: {sha}") from exc
        commit_tree = commit.tree
        parent_tree = r[commit.parents[0]].tree if commit.parents else None

        artifacts: dict[str, str] = {}
        old_paths: set[str] = set()
        deletions: list[str] = []
        changes = list(tree_changes(r.object_store, parent_tree, commit_tree))
        for ch in changes:
            # revert = restore the PARENT's state: rewrite each path that existed in
            # the parent back to the parent blob, and delete any path the commit added.
            if ch.old and ch.old.path:
                p = ch.old.path.decode()
                artifacts[p] = r[ch.old.sha].data.decode("utf-8")
                old_paths.add(p)
        for ch in changes:
            if ch.new and ch.new.path:
                p = ch.new.path.decode()
                if p not in old_paths:
                    deletions.append(p)
        diff_text = _revert_diff(r, commit_tree, artifacts, deletions)

    if not artifacts and not deletions:
        click.echo("nothing to revert (commit changed no files).")
        return
    reason = _resolve_reason(reason, allow_prompt=not yes)
    _guard(repo_path=repo_path, diff_text=diff_text, yes=yes)
    short = sha[:7]
    msg = _break_glass_message("ops", short, f"revert {short}", actor, reason)
    result = crud.repo.publish(artifacts, msg, deletions=deletions)
    click.echo(f"committed {result.commit_sha or '(unchanged)'}")
    _maybe_push(repo_path, push=push, yes=yes)


def _blob_text(r, tree, rel: str) -> str:
    """Text of ``rel`` at a tree, or '' when absent."""
    from dulwich.object_store import tree_lookup_path

    if tree is None:
        return ""
    try:
        _, blob_sha = tree_lookup_path(r.get_object, tree, rel.encode())
        return r[blob_sha].data.decode("utf-8", errors="replace")
    except KeyError:
        return ""


def _revert_diff(r, commit_tree, artifacts: dict[str, str], deletions: list[str]) -> str:
    """Unified diff from the commit's content (current) to the reverted content."""
    parts: list[str] = []
    for p, new_text in sorted(artifacts.items()):
        cur = _blob_text(r, commit_tree, p)
        parts.append(
            "".join(
                difflib.unified_diff(
                    cur.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=f"a/{p}",
                    tofile=f"b/{p}",
                )
            )
        )
    for p in sorted(deletions):
        cur = _blob_text(r, commit_tree, p)
        parts.append(
            "".join(
                difflib.unified_diff(
                    cur.splitlines(keepends=True), [], fromfile=f"a/{p}", tofile=f"b/{p} (deleted)"
                )
            )
        )
    return "\n".join(x for x in parts if x) or "(no textual change)"


@local_group.command("restore")
@click.argument("cls")
@click.argument("name")
@click.argument("version", type=int)
@_write_options
@_friendly
def restore_cmd(
    cls: str,
    name: str,
    version: int,
    repo: str | None,
    actor: str | None,
    reason: str | None,
    yes: bool,
    push: bool | None,
) -> None:
    """Roll a versioned resource back to an earlier published version.

    Mirrors VersionedDoc.rollback (point ``current`` at the version, keeping the
    immutable version envelope intact) but writes a BREAK-GLASS-marked commit -
    rollback itself would emit an ordinary conforming commit with no reason trailer.
    """
    crud = _build_crud(repo)
    actor = _resolve_actor(actor)
    versions = VersionedDoc(crud).list_versions(cls, name)  # ValueError if not versioned
    if version not in versions:
        raise click.ClickException(f"{cls}/{name}: no version {version} (have {versions})")
    before = crud.get(cls, name)
    after = copy.deepcopy(before)
    after["current"] = int(version)
    after["status"] = "published"
    reason = _resolve_reason(reason, allow_prompt=not yes)
    _guard(repo_path=crud.repo_path, diff_text=_yaml_diff(before, after), yes=yes)
    msg = _bg_message_for_class(crud, cls, name, f"restore v{version}", actor, reason)
    result = crud.put(cls, name, after, actor, message=msg)
    click.echo(f"committed {result.commit_sha or '(unchanged)'}")
    _maybe_push(crud.repo_path, push=push, yes=yes)


@local_group.command("push")
@_repo_option
@_friendly
def push_cmd(repo: str | None) -> None:
    """Push the clone's local (break-glass) commits to the configured remote."""
    _do_push(_resolve_repo_path(repo))


# --- ch-cloud (mounted scalo Typer) + root mounting --------------------------


def _mount_ch_cloud() -> None:
    """Mount the ch-cloud lifecycle Typer under ``dfe local ch-cloud``.

    ch_cloud_app is a scalo Typer; typer.main.get_command bridges it to a click
    command so it slots straight under this click group (it also runs daemon-free,
    the same break-glass posture as the rest of this group). Guarded so a bridge/
    import hiccup can never stop the core break-glass commands loading.
    """
    try:
        import typer.main

        from dfe_engine.cli.ch_cloud import ch_cloud_app

        local_group.add_command(typer.main.get_command(ch_cloud_app), name="ch-cloud")
    except Exception:  # ch-cloud is optional; never break the core group
        pass


_mount_ch_cloud()


def attach_local(root: click.Group) -> None:
    """Mount the ``local`` break-glass group onto the root ``dfe`` command."""
    root.add_command(local_group)
