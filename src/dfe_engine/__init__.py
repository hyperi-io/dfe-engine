#  Project:      dfe-engine
#  File:         __init__.py
#  Purpose:      Package initialization and version
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""DFE Engine - Core library for Data Fusion Engine.

Modules:
    source      — Source model, type registry, source registry
    schema      — Schema v2 YAML→DDL pipeline
    clickhouse  — ClickHouse client management
    pipeline    — Vector pipeline generation
    hunts       — Hunt scheduling and execution
    sigma       — Sigma rule conversion and field mapping
    services    — Service config registry and source routing
    helm        — Helm values compiler and Argo CD generators
    deployment  — K8s deployment config models
    auth        — Engine RBAC
    query       — Query registry and execution
    settings    — Pydantic settings cascade
"""

from importlib.metadata import version

__version__ = version("dfe-engine")
