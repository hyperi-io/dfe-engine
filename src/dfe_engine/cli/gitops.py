#  Project:      dfe-engine
#  File:         cli/gitops.py
#  Purpose:      `dfe-api gitops` subcommands -- render + publish deploy artifacts
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``dfe-api gitops`` subcommands.

Renders the engine's declarative artifacts (Argo apps/appproject, RBAC CSV, Helm
values, CH DDL) and commits them to the deploy-specific gitops repo. Argo CD
applies them -- the engine needs only git-write. Disabled unless
``DFE_GITOPS_ENABLED=true``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer
from scalo.cli import Typer
from scalo.cli.output import (
    print_error,
    print_info,
    print_success,
    print_table,
    print_warning,
)

from dfe_engine.gitops import GitopsRepo, collect_deploy_artifacts
from dfe_engine.settings import DFESettings, load_settings

if TYPE_CHECKING:
    from dfe_engine.helm.models import CompilationResult

gitops_app = Typer(help="Render + publish deploy artifacts to the gitops repo.")


def _build_compilation(settings: DFESettings):
    """Build the compiler from configured registries and run compile_all().

    Returns ``(CompilationResult, environment_name)``. Raises ``typer.Exit`` with
    a clear message when required config is missing.
    """
    if not settings.helm.environment_file:
        print_error("No environment file configured (set DFE_HELM_ENVIRONMENT_FILE).")
        raise typer.Exit(1)
    if not settings.deployment.config_dir:
        print_error("No deployment config dir configured (set DFE_DEPLOYMENT_CONFIG_DIR).")
        raise typer.Exit(1)
    if not settings.services.config_yaml_dir:
        print_error("No services config dir configured (set DFE_SERVICES_CONFIG_YAML_DIR).")
        raise typer.Exit(1)
    if not settings.source.sources_dir:
        print_error("No sources dir configured (set DFE_SOURCES_DIR).")
        raise typer.Exit(1)

    from dfe_engine.deployment.registry import DeploymentConfigRegistry
    from dfe_engine.helm import EnvironmentConfig, HelmValuesCompiler
    from dfe_engine.services.registry import ServiceConfigRegistry
    from dfe_engine.source.registry import SourceRegistry

    env = EnvironmentConfig.from_yaml(settings.helm.environment_file)
    compiler = HelmValuesCompiler(
        DeploymentConfigRegistry(config_directory=settings.deployment.config_dir),
        ServiceConfigRegistry(config_directory=settings.services.config_yaml_dir),
        SourceRegistry(sources_directory=settings.source.sources_dir),
        env,
    )
    return compiler.compile_all(), env.name


def _render_ddl(topology: str = "single") -> dict[str, str]:
    """Render bundled DDL; non-fatal if schema generation fails."""
    try:
        from dfe_engine.schema.ddl_writer import DDLFileWriter

        return DDLFileWriter(topology=topology).generate_all()
    except Exception as exc:  # DDL is optional in the artifact set
        print_warning(f"Skipping DDL render: {exc}")
        return {}


def _read_committed_values(repo_path) -> dict[str, str]:
    """Read the already-COMMITTED ``values/*.yaml`` from the local clone's HEAD.

    Passed to ``collect_deploy_artifacts(existing=...)`` so a publish MERGES the
    registry base under an operator's committed /helm var edits instead of
    reverting them. Reads from the git HEAD tree, NOT the working tree, so a stray
    uncommitted file (or one mid-write) can never be mistaken for a committed
    operator edit (P3.8). Empty repo / no HEAD / no ``values/`` -> empty map (a
    first publish).
    """
    from typing import cast

    from dulwich.errors import NotGitRepository
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import Repo

    try:
        with Repo(str(repo_path)) as repo:
            head_commit = cast("Commit", repo[repo.head()])
            root = cast("Tree", repo[head_commit.tree])
            if b"values" not in root:
                return {}
            _, values_sha = root[b"values"]
            values_tree = cast("Tree", repo[values_sha])
            out: dict[str, str] = {}
            for entry in values_tree.items():
                name = entry.path.decode()
                if name.endswith(".yaml"):
                    out[f"values/{name}"] = cast("Blob", repo[entry.sha]).data.decode()
            return out
    except (KeyError, FileNotFoundError, NotGitRepository):
        return {}


def _render_ch_rbac_ddl() -> list[str]:
    """Render the survivability CH-RBAC DDL: quota tiers + fixed service ROLES.

    Pass to ``collect_deploy_artifacts(ch_rbac_ddl=...)`` so ``ddl/ch-rbac.sql``
    lands in the deploy repo - the git-only seam that lets the shared migration
    runner rebuild CH RBAC if the engine is absent. Only the CONFIG-only objects
    are rendered here (the reconciler's ``render_all`` is pure for tiers +
    service roles): the tenant row policies need live ``_org_id`` table discovery
    and the fixed/service USER secrets need the secrets seam, so both are
    intentionally skipped (empty ``org_tables`` + empty hashes) - the live
    reconciler fills them in. NB the generated SQL also assumes the CH server has
    ``<custom_settings_prefixes>DFE_</custom_settings_prefixes>`` (deploy config,
    see .env.example). Non-fatal: a render failure warns and drops the DDL.
    """
    try:
        from dfe_engine.governance.ch import (
            DEFAULT_SERVICE_ROLES,
            DEFAULT_TIERS,
            ChRbacReconciler,
        )

        # admin_client=None is safe: render_all is pure, it never touches the client.
        return ChRbacReconciler(None).render_all(
            tiers=DEFAULT_TIERS,
            service_roles=DEFAULT_SERVICE_ROLES,
            org_tables=[],
            service_hashes={},
            fixed_hashes={},
        )
    except Exception as exc:  # CH-RBAC DDL is optional in the artifact set
        print_warning(f"Skipping CH-RBAC DDL render: {exc}")
        return []


def _render_hyperdx_connections(settings: DFESettings) -> str | None:
    """Build the HyperDX per-ORG DEFAULT_CONNECTIONS JSON from the org registry.

    Loads the org registry, the connection config (for the shared network
    coordinates), and the scalo.secrets seam - mirroring app.py's bootstrap wiring -
    then emits one connection per org via ``build_hyperdx_connections_json``: all
    share the fixed ``dfe_tenant_reader`` user, each row-scoped by its baked-in
    ``DFE_current_tenant_id`` setting. Returns None (skip the artifact) when the base
    ``default`` connection is unavailable. Non-fatal: a failure warns + skips.
    """
    try:
        import os

        from dfe_engine.connections.config import ConnectionConfigLoader
        from dfe_engine.hyperdx.client import build_hyperdx_connections_json
        from dfe_engine.orgs.registry import OrgRegistry
        from dfe_engine.secrets import build_secrets

        config_dir = settings.config_dir or os.environ.get("DFE_CONFIG_DIR", "")
        base_dir = Path(config_dir) if config_dir else Path("config")

        conn_path = base_dir / "rbac" / "connections.yaml"
        conn_config = (
            ConnectionConfigLoader.load(conn_path)
            if conn_path.exists()
            else ConnectionConfigLoader.load_default()
        )
        base = conn_config.connections.get("default")
        if base is None:
            return None

        orgs = OrgRegistry(base_dir / "orgs").list()
        secrets_store = build_secrets(settings.secrets)
        return build_hyperdx_connections_json(orgs, base=base, secrets_store=secrets_store)
    except Exception as exc:  # HyperDX connections are optional in the artifact set
        print_warning(f"Skipping HyperDX connections render: {exc}")
        return None


def _render_hyperdx_sources(settings: DFESettings) -> str | None:
    """Build the HyperDX DEFAULT_SOURCES JSON: one ``log`` source per built source table.

    A source owns its own ClickHouse table once its deployed/runtime version carries a
    ``meta_schema`` (sources without one still land in the shared catch-all table and
    are skipped here). Emits ``(effective_data_database, source.table_name)`` for each
    such source via ``build_hyperdx_sources_json``, referencing the first enabled org's
    connection (the fork fans the sources out across the per-org connections - see the
    builder's fork-dependency note). Returns None (skip) when there is no sources dir,
    no org connection to reference, or no built source table. Non-fatal: warns + skips.
    """
    try:
        import os

        from dfe_engine.hyperdx.client import build_hyperdx_sources_json
        from dfe_engine.orgs.registry import OrgRegistry
        from dfe_engine.source.registry import SourceRegistry

        if not settings.source.sources_dir:
            return None

        config_dir = settings.config_dir or os.environ.get("DFE_CONFIG_DIR", "")
        base_dir = Path(config_dir) if config_dir else Path("config")

        orgs = OrgRegistry(base_dir / "orgs").list()
        connection = next((o.name for o in orgs if getattr(o, "enabled", True)), "")
        if not connection:
            return None

        db = settings.clickhouse.effective_data_database
        registry = SourceRegistry(sources_directory=settings.source.sources_dir)
        source_tables: list[tuple[str, str]] = []
        for meta in registry.list_sources(enabled_only=True):
            source = registry.get_source(meta["source"])
            if source.schema_config.meta_schema:
                source_tables.append((db, source.table_name))
        if not source_tables:
            return None
        return build_hyperdx_sources_json(source_tables, connection=connection)
    except Exception as exc:  # HyperDX sources are optional in the artifact set
        print_warning(f"Skipping HyperDX sources render: {exc}")
        return None


def _assemble_artifacts(
    settings: DFESettings, result: CompilationResult, existing: dict[str, str]
) -> dict[str, str]:
    """Assemble the full deploy-repo artifact map for a publish.

    Compiled Helm overlay values + schema DDL + CH-RBAC survivability DDL + Envoy
    OIDC values + HyperDX per-group connections. Split out of ``publish`` so it
    needs no git clone/commit and is unit-testable with a real (even empty)
    ``CompilationResult`` and on-disk registries.
    """
    artifacts = collect_deploy_artifacts(
        result,
        ddl=_render_ddl(settings.clickhouse.topology),
        ch_rbac_ddl=_render_ch_rbac_ddl(),
        existing=existing,
    )

    # Engine is the OIDC SSoT: render Envoy oidc-values.yaml from the provider
    # registry into the deploy repo (the envoy app pulls it via $values).
    if settings.auth.oidc.providers_dir:
        from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
        from dfe_engine.gitops import (
            ENVOY_OIDC_VALUES_PATH,
            build_oidc_providers,
            render_envoy_oidc_values,
        )

        oidc_registry = OIDCProviderRegistry(Path(settings.auth.oidc.providers_dir))
        providers = build_oidc_providers(oidc_registry)
        artifacts[ENVOY_OIDC_VALUES_PATH] = render_envoy_oidc_values(providers)

    # HyperDX consumes one ClickHouse connection per ORG (all share the fixed
    # dfe_tenant_reader user, each row-scoped by its baked-in DFE_current_tenant_id
    # setting); render the per-org DEFAULT_CONNECTIONS JSON into the deploy repo.
    connections = _render_hyperdx_connections(settings)
    if connections is not None:
        from dfe_engine.hyperdx.client import HYPERDX_CONNECTIONS_PATH

        artifacts[HYPERDX_CONNECTIONS_PATH] = connections

    # One HyperDX `log` source per built source table (DEFAULT_SOURCES) so a newly
    # deployed table is dashboard-queryable straight away (Task F, GA integration).
    sources = _render_hyperdx_sources(settings)
    if sources is not None:
        from dfe_engine.hyperdx.client import HYPERDX_SOURCES_PATH

        artifacts[HYPERDX_SOURCES_PATH] = sources

    return artifacts


@gitops_app.command("publish")
def publish() -> None:
    """Compile and commit deploy artifacts to the gitops repo."""
    settings = load_settings()
    gitops = settings.gitops

    if not gitops.enabled:
        print_warning("Gitops publishing is disabled (set DFE_GITOPS_ENABLED=true).")
        raise typer.Exit(0)
    if not gitops.local_path:
        print_error("No gitops working path configured (set DFE_GITOPS_LOCAL_PATH).")
        raise typer.Exit(1)

    result, environment = _build_compilation(settings)
    if result.errors:
        for err in result.errors:
            print_error(err)
        raise typer.Exit(1)
    for warn in result.warnings:
        print_warning(warn)

    repo = GitopsRepo(
        local_path=gitops.local_path,
        repo_url=gitops.repo_url,
        branch=gitops.branch,
        push=gitops.push,
        username=gitops.username,
        token=gitops.token,
        author_name=gitops.author_name,
        author_email=gitops.author_email,
    )
    repo.ensure()

    # Clone first, THEN read the committed values so the merge preserves an
    # operator's /helm var edits (registry base merges UNDER the committed
    # overlay); a bare regenerate would silently revert them on every publish.
    existing = _read_committed_values(repo.path)
    artifacts = _assemble_artifacts(settings, result, existing)

    outcome = repo.publish(artifacts, message=f"chore: publish {environment} deploy artifacts")

    if not outcome.changed:
        print_info("No changes; gitops repo already up to date.")
        return
    print_success(
        f"Published {len(outcome.files)} artifact(s); commit {outcome.commit_sha}"
        + (" (pushed)" if outcome.pushed else "")
    )


@gitops_app.command("status")
def status() -> None:
    """Show the resolved gitops settings (token masked)."""
    g = load_settings().gitops
    rows = [
        ["enabled", str(g.enabled)],
        ["repo_url", g.repo_url or "(unset)"],
        ["branch", g.branch],
        ["local_path", g.local_path or "(unset)"],
        ["push", str(g.push)],
        ["username", g.username or "(unset)"],
        ["token", "***" if g.token else "(unset)"],
        ["author", f"{g.author_name} <{g.author_email}>"],
    ]
    print_table(rows, title="Gitops settings", headers=["setting", "value"])


@gitops_app.command("log")
def gitops_log(
    limit: int = typer.Option(50, help="Max entries to read."),
    group_by: str = typer.Option(
        "", help="Group summary: scope|actor|type|day (empty = flat log)."
    ),
) -> None:
    """Show the gitcrud audit log (all engine git changes, newest first)."""
    from datetime import UTC, datetime

    from dfe_engine.gitcrud.factory import build_gitcrud
    from dfe_engine.gitcrud.log import group_log, read_log

    settings = load_settings()
    crud = build_gitcrud(settings.gitops)
    if crud is None:
        print_error("Gitops is not enabled (set DFE_GITOPS_ENABLED + DFE_GITOPS_LOCAL_PATH).")
        raise typer.Exit(1)
    entries, _ = read_log(crud, limit=limit)
    if not entries:
        print_info("No gitcrud history yet.")
        return
    if group_by:
        groups = group_log(entries, group_by)
        rows = [[g["key"], str(g["count"]), g["latest"].summary, g["latest"].actor] for g in groups]
        print_table(
            rows, title="Gitcrud audit log (grouped)", headers=["Key", "Count", "Latest", "Actor"]
        )
        return
    rows = [
        [
            e.sha[:7],
            datetime.fromtimestamp(e.timestamp, tz=UTC).strftime("%Y-%m-%d %H:%M"),
            e.ctype or "-",
            e.scope or "-",
            e.actor,
            e.state,
            e.summary,
        ]
        for e in entries
    ]
    print_table(
        rows,
        title="Gitcrud audit log",
        headers=["SHA", "When (UTC)", "Type", "Scope", "Actor", "State", "Summary"],
    )


def register_gitops_commands(app: Typer) -> None:
    """Register the ``gitops`` subcommand group on *app*."""
    app.add_typer(gitops_app, name="gitops")
