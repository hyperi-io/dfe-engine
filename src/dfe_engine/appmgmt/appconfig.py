#  Project:      dfe-engine
#  File:         appmgmt/appconfig.py
#  Purpose:      Render an app's native config file where no chart does it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The step a chart performs on Kubernetes, performed here where there is none.

An app instance IS its values overlay, and on Kubernetes the app's own chart
turns that overlay into a ConfigMap the pod mounts. A Compose deployment runs no
chart, so the overlay stopped at the deploy repo: the engine wrote the receiver's
compiled routing and the running receiver never saw it.

This module is that missing step. For each app the deployment runs it merges the
instance overlay's ``config:`` block over the base config the deployment injected
and writes the app's own config file into one directory per app, which Compose
mounts read-only into the container. The base is the deployment's - brokers,
warehouse credentials, the dead-letter spool - and never reaches the deploy repo;
the overlay is the governed half and wins wherever the two name the same key.

A per-config app gets one directory per INSTANCE - ``<service>/<instance>/`` -
because it runs one container per source and they must not read each other's
config. The names are also written to ``<service>.instances`` beside the env
files, which is the one host-visible thing the engine writes on Compose: the
deployer reads it to declare a container per instance, the way the layer2-apps
ApplicationSet reads an Application per overlay on Kubernetes.

Two facts make it generic. Which file an app reads is the manifest's
``consumes.config``, and how a rendered file set reaches the app is the file
set's ``dir_setting`` (one directory) or ``entries_path`` (one entry per file),
so an app joins by being declared rather than by a branch here.

The overlay's ``extraEnv:`` block is rendered the same way, into one env file per
app that Compose reads as a second ``env_file`` entry. Kubernetes needs neither
step: there the app's chart turns both blocks into a ConfigMap and container
environment, and this module does nothing at all.

Nothing restarts a container. A write the app cannot take in place is REPORTED,
with the command that applies it, because a Compose stack's supervisor is the
operator and an engine holding the docker socket would be a second one.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scalo.logger import logger

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.engine import ResourceNotFoundError, get_path, set_path
from dfe_engine.yaml_utils import deep_merge, yaml_dump_string, yaml_load

from . import files, instances
from .catalogue import APP_CATALOGUE, AppDescriptor, ConsumedFileSet, Multiplicity, ReloadMode
from .scaling import DeployTarget

CONFIG_ROOT = "config"
"""The overlay key holding what becomes the app's own config file.

Every ``dir_setting`` and ``entries_path`` in the manifest is an OVERLAY path,
so it carries this prefix; the rendered file is the block itself, and the prefix
comes off on the way in.
"""

ENV_ROOT = "extraEnv"
"""The overlay key holding environment names no app's contract declares.

A sibling of ``config:`` rather than a branch of it: the app's own schema decides
what belongs under ``config:``, and this block exists for what it does not know
about. On Kubernetes the app's chart renders it; here it becomes a file.
"""

CUSTOM_ENV_SUFFIX = ".custom.env"
"""One file per app, named apart from the operator's own ``<app>.env``."""

INSTANCE_INDEX_SUFFIX = ".instances"
"""One file per per-config app, listing the instances it has rendered config for.

The deployer declares a container per line. A file with no lines is an app with
no source yet, which is a deployment that starts with none of that app running.
"""

APPLY_COMMAND = "make apply SERVICES={service}"
"""What a Compose operator runs in the deployment's checkout to apply a render.

The target re-resolves the stack before it starts anything, so it names a
per-source container this render has only just declared, which a plain
``docker compose`` call cannot: that service lives in a file only ``make``
chains.
"""

RESTART_HINT = f"restart required: {APPLY_COMMAND}"
"""What an operator runs to apply a write the running app cannot take in place."""

RECREATE_HINT = f"recreate required: {APPLY_COMMAND}"
"""Compose reads env_file at up time, so a restart keeps the old environment."""


class AppConfigError(RuntimeError):
    """Raised when a rendered config cannot be written."""


@dataclass(frozen=True, slots=True)
class RenderedApp:
    """One app's rendered config, and what taking it costs."""

    service: str
    instance: str
    """The overlay this was rendered from; empty where the app has none yet."""

    directory: Path
    changed: bool
    restart_required: bool
    custom_env_changed: bool = False
    per_instance: bool = False
    """Whether this app runs a container per instance, which names the container."""

    created: bool = False
    """Whether this instance is new here, so no container is running it yet."""

    @property
    def container(self) -> str:
        """The Compose service carrying this render, which is what an operator acts on."""
        if self.per_instance and self.instance:
            return f"{self.service}-{self.instance}"
        return self.service

    @property
    def restart_hint(self) -> str:
        """The command that applies this change, or empty when none is needed."""
        # A changed env file changes the service definition, which `up` recreates
        # on, and `up` is also what CREATES the container a new instance lacks.
        if self.custom_env_changed or self.created:
            return RECREATE_HINT.format(service=self.container)
        if not self.restart_required:
            return ""
        return RESTART_HINT.format(service=self.container)


def enabled(settings: Any) -> bool:
    """Whether this deployment renders its apps' config files itself.

    True only where BOTH halves are in place: a target with no chart to do it,
    and a deployer that named the directory its containers mount. A Compose
    stack that wires neither is reported as applying no routing rather than
    claiming a delivery it does not make.
    """
    if DeployTarget(settings.deployment.target) is not DeployTarget.DOCKER:
        return False
    return bool(str(settings.deployment.app_config_dir or "").strip())


def _out_root(settings: Any) -> Path:
    return Path(str(settings.deployment.app_config_dir).strip())


def _mount_root(settings: Any) -> str:
    """Where the APPS see the rendered tree.

    A different path from the one written to, because the engine and the app
    mount the same directory at their own paths. A deployer that names only one
    is saying they are the same.
    """
    return str(settings.deployment.app_config_mount or settings.deployment.app_config_dir).strip()


def _base_config(settings: Any, service: str) -> dict[str, Any]:
    """The deployment's own config for this app, or an empty base where it names none.

    Injected rather than stored: brokers, warehouse credentials and spool paths
    belong to the deployment, and a copy of them in the deploy repo would be a
    second place to keep them right.
    """
    base_dir = str(settings.deployment.app_config_base_dir or "").strip()
    if not base_dir:
        return {}
    path = Path(base_dir) / f"{service}.yaml"
    if not path.is_file():
        return {}
    loaded = yaml_load(path)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise AppConfigError(f"{path} does not hold a mapping, so it is not a config for {service}")
    return dict(loaded)


def _config_block(doc: dict) -> dict[str, Any]:
    """The overlay's ``config:`` block, which is what the app reads."""
    block = doc.get(CONFIG_ROOT)
    return dict(block) if isinstance(block, dict) else {}


def custom_env(doc: dict) -> dict[str, Any]:
    """The overlay's ``extraEnv:`` block, which becomes container environment."""
    block = doc.get(ENV_ROOT)
    return dict(block) if isinstance(block, dict) else {}


def custom_env_dir(settings: Any) -> Path | None:
    """The directory this deployment's containers read their env files from.

    Named only by a deployer that has no chart to render ``extraEnv`` for it, so
    an unset value is how Kubernetes says the chart does that job instead.
    """
    named = str(settings.deployment.app_env_dir or "").strip()
    return Path(named) if named else None


def _env_line(key: str, value: Any) -> str:
    """One ``KEY=value`` line, with a bool spelled the way an app parses one."""
    if isinstance(value, bool):
        return f"{key}={'true' if value else 'false'}\n"
    if value is None:
        return f"{key}=\n"
    return f"{key}={value}\n"


def _write_private(path: Path, content: str) -> None:
    """Replace ``path`` in one step with a file only its owner can read."""
    tmp = path.with_name(f".{path.name}.tmp")
    handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(content)
    tmp.replace(path)


def _report_unwritable(what: str, directory: Path, error: OSError) -> None:
    """Report a directory the deployer named and this engine cannot write.

    The config render is what the containers read and must survive it: the env
    directory is the operator's own checkout, so a mode or an owner there is
    theirs to fix and must not cost every app its config.
    """
    logger.warning(
        "the directory this deployment names for its app env files cannot be written",
        wrote=what,
        directory=str(directory),
        error=str(error),
    )


def write_custom_env(settings: Any, service: str, env: dict[str, Any]) -> bool:
    """Write one app's custom environment where its container reads it.

    Returns whether the file changed, because a Compose service takes a new
    env_file on ``up`` and not on ``restart``, so the operator has to be told
    which of the two applies.

    Private mode: an operator writes credentials here, and the file sits in their
    own checkout rather than in the deploy repo.
    """
    directory = custom_env_dir(settings)
    if directory is None:
        return False
    target = directory / f"{service}{CUSTOM_ENV_SUFFIX}"
    rendered = "".join(_env_line(key, value) for key, value in sorted(env.items()))
    # An app that has never had a custom key gets no file at all, so a fresh stack
    # does not hand its operator one recreate command per app before it has run.
    if not rendered and not target.is_file():
        return False
    if target.is_file() and target.read_text(encoding="utf-8") == rendered:
        return False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _write_private(target, rendered)
    except OSError as exc:
        _report_unwritable(target.name, directory, exc)
        return False
    return True


def _inner_path(path: str) -> str:
    """A manifest dot-path as it reads INSIDE the rendered file."""
    prefix = f"{CONFIG_ROOT}."
    return path[len(prefix) :] if path.startswith(prefix) else path


def _enable_reload(app: AppDescriptor, config: dict[str, Any]) -> bool:
    """Turn the app's config watcher on where it ships off, and say whether it reloads.

    A value the deployment already set is kept, so an operator can pin the app;
    one set false then costs a restart per change, which is what is reported.
    """
    if not app.hot_reload:
        return False
    if not app.reload_setting:
        return True
    inner = _inner_path(app.reload_setting)
    current = get_path(config, inner, default=None)
    if current is None:
        set_path(config, inner, True)
        return True
    return current is True


def _write_file_set(directory: Path, entries: list[files.AppFile]) -> bool:
    """Replace the set's directory with exactly what the overlay carries.

    Replaced rather than updated: a program deleted from the overlay has to
    leave the directory too, and dfe-transform-vrl compiles every ``.vrl`` it
    finds into one program, so a leftover file keeps running.
    """
    wanted = {entry.name: entry.content for entry in entries}
    present: dict[str, str] = {}
    if directory.is_dir():
        present = {
            p.name: p.read_text(encoding="utf-8") for p in directory.iterdir() if p.is_file()
        }
    if present == wanted:
        return False
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in wanted.items():
        (directory / name).write_text(content, encoding="utf-8")
    return True


def _apply_file_sets(
    app: AppDescriptor,
    doc: dict,
    config: dict[str, Any],
    app_dir: Path,
    mount_dir: str,
) -> list[ConsumedFileSet]:
    """Render every declared file set and point the config at what was written.

    Returns the sets whose content changed, so the caller can say whether the
    app can take the change where it stands.
    """
    changed: list[ConsumedFileSet] = []
    for file_set in app.files:
        entries = files.list_files(doc, file_set)
        directory = app_dir / file_set.name
        if _write_file_set(directory, entries):
            changed.append(file_set)
        mounted = f"{mount_dir}/{file_set.name}"
        if file_set.dir_path:
            set_path(config, _inner_path(file_set.dir_path), mounted)
        elif file_set.entries_path:
            _apply_entries(config, file_set, entries, mounted)
    return changed


def _apply_entries(
    config: dict[str, Any], file_set: ConsumedFileSet, entries: list[files.AppFile], mounted: str
) -> None:
    """Name each rendered file in the app's own entry list, keeping what it declares.

    An entry the config already names is left alone: that one carries the
    author's key columns, and replacing it with a derived entry would turn every
    lookup into a full scan. The name is the file name without its extension,
    which is the name a program looks the table up by.

    A declared entry whose path sits in this set's directory and names a file the
    set no longer carries is kept and warned about: this render just rewrote that
    directory, so the path is known to be dead, while an entry pointing anywhere
    else may name a file the app receives another way.
    """
    inner = _inner_path(file_set.entries_path)
    declared = config
    for part in inner.split("."):
        declared = declared.get(part) if isinstance(declared, dict) else None
        if declared is None:
            break
    existing = list(declared) if isinstance(declared, list) else []
    carried = {entry.name for entry in entries}
    for entry in existing:
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str) or not path.startswith(f"{mounted}/"):
            continue
        missing = path.removeprefix(f"{mounted}/")
        if missing not in carried:
            logger.warning(
                "a declared entry names a file this set no longer carries",
                file_set=file_set.name,
                entry=entry.get("name"),
                missing_file=missing,
                path=path,
            )
    named = {e.get("name") for e in existing if isinstance(e, dict)}
    derived = [
        {"name": entry.name.rsplit(".", 1)[0], "path": f"{mounted}/{entry.name}"}
        for entry in entries
        if entry.name.rsplit(".", 1)[0] not in named
    ]
    if existing or derived:
        set_path(config, inner, existing + derived)


def _instances_for(gc: GitCrud, app: AppDescriptor) -> list[instances.AppInstance | None]:
    """Every overlay this app runs a container from here, in a stable order.

    A per-config app runs one container per source, so each of its overlays is
    rendered into a directory of its own and the deployer declares a container
    per name. A single-deployment app runs one container whatever the deploy repo
    holds, so a second overlay for it is a manifest and a repo that disagree: the
    first is used and the rest are named in the log rather than silently applied.

    ``None`` is an app with no overlay at all, which still renders the
    deployment's own base config for a container the profile started idle.
    """
    deployed = instances.list_instances(gc, service=app.service)
    if not deployed:
        return [None]
    if app.multiplicity is Multiplicity.PER_CONFIG:
        return list(deployed)
    if len(deployed) > 1:
        logger.warning(
            "more overlays than this target runs containers for; the rest are not rendered",
            app=app.service,
            rendered=deployed[0].instance,
            ignored=[i.instance for i in deployed[1:]],
        )
    return [deployed[0]]


def write_instance_index(settings: Any, service: str, names: list[str]) -> None:
    """Record which instances of a per-config app have rendered config here.

    The one host-visible thing the engine writes on Compose: the deployer reads
    it to declare a container per name, the way the layer2-apps ApplicationSet
    reads an Application per overlay on Kubernetes. Written even when empty, so a
    deployment whose last source of this kind went away stops declaring one.
    """
    directory = custom_env_dir(settings)
    if directory is None:
        return
    target = directory / f"{service}{INSTANCE_INDEX_SUFFIX}"
    rendered = "".join(f"{name}\n" for name in names)
    if target.is_file() and target.read_text(encoding="utf-8") == rendered:
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        _report_unwritable(target.name, directory, exc)


def _render_one(
    gc: GitCrud,
    app: AppDescriptor,
    instance: instances.AppInstance | None,
    settings: Any,
    out_root: Path,
    mount_root: str,
) -> RenderedApp:
    """Write one instance's config file and file sets, and report what taking it costs."""
    doc: dict = {}
    if instance is not None:
        try:
            doc = instances.read_overlay(gc, instance)
        except ResourceNotFoundError:
            doc = {}

    # A per-config app's container reads its OWN instance directory, so two
    # sources of one connector never share a config file.
    per_instance = app.multiplicity is Multiplicity.PER_CONFIG and instance is not None
    leaf = f"{app.service}/{instance.instance}" if per_instance else app.service

    config = deep_merge(_base_config(settings, app.service), _config_block(doc), replace_lists=True)
    reloads = _enable_reload(app, config)
    app_dir = out_root / leaf
    # A directory this render creates has no container reading it yet, so the
    # first write of a stack's whole app config is never a restart.
    first_write = not app_dir.exists()
    mount_dir = f"{mount_root}/{leaf}"
    changed_sets = _apply_file_sets(app, doc, config, app_dir, mount_dir)

    env_changed = write_custom_env(settings, app.service, custom_env(doc))

    target = app_dir / app.config_file
    rendered = yaml_dump_string(config)
    config_changed = not target.is_file() or target.read_text(encoding="utf-8") != rendered
    # Rewritten when only a file set changed, so the file's mtime moves: the
    # config file is the app's trigger surface, and a program appearing in a
    # directory it already names is invisible to a watcher polling that file.
    if config_changed or changed_sets:
        app_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, rendered)

    # Only the app knows which of its settings are startup-bound, so the coarse
    # manifest fact is what is reported: a hot-reloading app takes a config
    # change where it stands, and every other write needs the process restarted.
    restart = not first_write and (
        (config_changed and not reloads)
        or any(fs.reload is not ReloadMode.HOT for fs in changed_sets)
    )
    # A new instance has no container yet, so the deployer creates one rather
    # than restarting anything: `up` reads the index this render just wrote.
    return RenderedApp(
        service=app.service,
        instance=instance.instance if instance is not None else "",
        directory=app_dir,
        changed=config_changed or bool(changed_sets) or env_changed,
        restart_required=restart,
        custom_env_changed=env_changed,
        per_instance=per_instance,
        created=per_instance and first_write,
    )


def _atomic_write(path: Path, content: str) -> None:
    """Replace ``path`` in one step, so a reader never sees a half-written config."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def renderable(settings: Any) -> list[AppDescriptor]:
    """Every app this deployment renders a config for, in a stable order.

    An app with no ``consumes.config`` reads no config file the engine renders -
    the console and the observability UI - and one this profile does not offer
    has no container here to render for.
    """
    profile = settings.deployment.profile
    return sorted(
        (a for a in APP_CATALOGUE.values() if a.config_file and a.offered_in(profile)),
        key=lambda a: a.service,
    )


def _prune_instance_dirs(app_root: Path, keep: list[str]) -> None:
    """Drop the directories of instances this deployment no longer has.

    A source deleted here has to take its rendered config with it: the deployer
    reads the index to stop declaring the container, and a config left behind
    would come back the moment anything re-declared one by that name.
    """
    if not app_root.is_dir():
        return
    wanted = set(keep)
    for path in app_root.iterdir():
        if path.is_dir() and path.name not in wanted:
            shutil.rmtree(path)


def render(gc: GitCrud, settings: Any) -> list[RenderedApp]:
    """Render every app's config from the deploy repo. Returns what was written.

    Safe to call on every write: an app whose rendered content is unchanged is
    reported with ``changed`` false and its file is not touched, so nothing
    restarts because the engine ran again.
    """
    if not enabled(settings):
        return []
    out_root = _out_root(settings)
    mount_root = _mount_root(settings)
    out_root.mkdir(parents=True, exist_ok=True)
    written: list[RenderedApp] = []
    for app in renderable(settings):
        found = _instances_for(gc, app)
        written.extend(
            _render_one(gc, app, instance, settings, out_root, mount_root) for instance in found
        )
        if app.multiplicity is not Multiplicity.PER_CONFIG:
            continue
        names = [i.instance for i in found if i is not None]
        _prune_instance_dirs(out_root / app.service, names)
        write_instance_index(settings, app.service, names)
    return written


def restart_hints(rendered: list[RenderedApp]) -> list[str]:
    """The command per app whose change the running process cannot take in place."""
    return [r.restart_hint for r in rendered if r.restart_hint]


def render_and_report(gc: GitCrud | None, settings: Any) -> list[str]:
    """Render, log what changed, and return the restart hints for the API response.

    Never raises: a deployment whose rendered directory is unwritable still has
    a working API and a deploy repo, and the next write renders it again.
    """
    if gc is None or not enabled(settings):
        return []
    try:
        rendered = render(gc, settings)
    except Exception as exc:  # the next write renders it again
        logger.warning("app config not rendered for the running containers", error=str(exc))
        return []
    for entry in rendered:
        if not entry.changed:
            continue
        logger.info(
            "Rendered an app's config for its running container",
            app=entry.service,
            instance=entry.instance,
            container=entry.container,
            restart=entry.restart_hint or "not needed",
        )
    return restart_hints(rendered)
