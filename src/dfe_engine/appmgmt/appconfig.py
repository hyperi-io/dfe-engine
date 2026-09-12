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

Two facts make it generic. Which file an app reads is the manifest's
``consumes.config``, and how a rendered file set reaches the app is the file
set's ``dir_setting`` (one directory) or ``entries_path`` (one entry per file),
so an app joins by being declared rather than by a branch here.

Nothing restarts a container. A write the app cannot take in place is REPORTED,
with the command that applies it, because a Compose stack's supervisor is the
operator and an engine holding the docker socket would be a second one.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scalo.logger import logger

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.engine import ResourceNotFoundError, set_path
from dfe_engine.yaml_utils import deep_merge, yaml_dump_string, yaml_load

from . import files, instances
from .catalogue import APP_CATALOGUE, AppDescriptor, ConsumedFileSet, ReloadMode
from .scaling import DeployTarget

CONFIG_ROOT = "config"
"""The overlay key holding what becomes the app's own config file.

Every ``dir_setting`` and ``entries_path`` in the manifest is an OVERLAY path,
so it carries this prefix; the rendered file is the block itself, and the prefix
comes off on the way in.
"""

RESTART_HINT = "restart required: docker compose restart {service}"
"""What an operator runs to apply a write the running app cannot take in place."""


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

    @property
    def restart_hint(self) -> str:
        """The command that applies this change, or empty when none is needed."""
        if not self.restart_required:
            return ""
        return RESTART_HINT.format(service=self.service)


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


def _inner_path(path: str) -> str:
    """A manifest dot-path as it reads INSIDE the rendered file."""
    prefix = f"{CONFIG_ROOT}."
    return path[len(prefix) :] if path.startswith(prefix) else path


def _write_file_set(directory: Path, entries: list[files.AppFile]) -> bool:
    """Replace the set's directory with exactly what the overlay carries.

    Replaced rather than updated: a program deleted from the overlay has to
    leave the directory too, and dfe-transform-vrl compiles every ``.vrl`` it
    finds into one program, so a leftover file keeps running.
    """
    wanted = {entry.name: entry.content for entry in entries}
    present: dict[str, str] = {}
    if directory.is_dir():
        present = {p.name: p.read_text(encoding="utf-8") for p in directory.iterdir() if p.is_file()}
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
    """
    inner = _inner_path(file_set.entries_path)
    declared = config
    for part in inner.split("."):
        declared = declared.get(part) if isinstance(declared, dict) else None
        if declared is None:
            break
    existing = list(declared) if isinstance(declared, list) else []
    named = {e.get("name") for e in existing if isinstance(e, dict)}
    derived = [
        {"name": entry.name.rsplit(".", 1)[0], "path": f"{mounted}/{entry.name}"}
        for entry in entries
        if entry.name.rsplit(".", 1)[0] not in named
    ]
    if existing or derived:
        set_path(config, inner, existing + derived)


def _instance_for(gc: GitCrud, service: str) -> instances.AppInstance | None:
    """The one overlay this app's single container runs from, or None.

    Compose declares its services in a committed file and creates none at run
    time, so a per-config app holds ONE deployment there and the engine caps it
    at one. A second overlay would have two configs racing for one container, so
    the first is used and the rest are named in the log rather than applied.
    """
    deployed = instances.list_instances(gc, service=service)
    if not deployed:
        return None
    if len(deployed) > 1:
        logger.warning(
            "more overlays than this target runs containers for; the rest are not rendered",
            app=service,
            rendered=deployed[0].instance,
            ignored=[i.instance for i in deployed[1:]],
        )
    return deployed[0]


def _render_one(
    gc: GitCrud, app: AppDescriptor, settings: Any, out_root: Path, mount_root: str
) -> RenderedApp:
    """Write one app's config file and file sets, and report what taking it costs."""
    instance = _instance_for(gc, app.service)
    doc: dict = {}
    if instance is not None:
        try:
            doc = instances.read_overlay(gc, instance)
        except ResourceNotFoundError:
            doc = {}

    config = deep_merge(_base_config(settings, app.service), _config_block(doc), replace_lists=True)
    app_dir = out_root / app.service
    # A directory this render creates has no container reading it yet, so the
    # first write of a stack's whole app config is never a restart.
    first_write = not app_dir.exists()
    mount_dir = f"{mount_root}/{app.service}"
    changed_sets = _apply_file_sets(app, doc, config, app_dir, mount_dir)

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
        (config_changed and not app.hot_reload)
        or any(fs.reload is not ReloadMode.HOT for fs in changed_sets)
    )
    return RenderedApp(
        service=app.service,
        instance=instance.instance if instance is not None else "",
        directory=app_dir,
        changed=config_changed or bool(changed_sets),
        restart_required=restart,
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
    return [_render_one(gc, app, settings, out_root, mount_root) for app in renderable(settings)]


def restart_hints(rendered: list[RenderedApp]) -> list[str]:
    """The command per app whose change the running process cannot take in place."""
    return [r.restart_hint for r in rendered if r.restart_required]


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
            restart=entry.restart_hint or "not needed",
        )
    return restart_hints(rendered)
