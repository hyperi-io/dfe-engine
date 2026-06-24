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

import typer
from hyperi_pylib.cli import Typer
from hyperi_pylib.cli.output import (
    print_error,
    print_info,
    print_success,
    print_table,
    print_warning,
)

from dfe_engine.gitops import GitopsRepo, collect_deploy_artifacts
from dfe_engine.settings import DFESettings, load_settings

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

    artifacts = collect_deploy_artifacts(
        result, environment=environment, ddl=_render_ddl(settings.clickhouse.topology)
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

        oidc_registry = OIDCProviderRegistry(settings.auth.oidc.providers_dir)
        providers = build_oidc_providers(oidc_registry)
        artifacts[ENVOY_OIDC_VALUES_PATH] = render_envoy_oidc_values(providers)

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


def register_gitops_commands(app: Typer) -> None:
    """Register the ``gitops`` subcommand group on *app*."""
    app.add_typer(gitops_app, name="gitops")
