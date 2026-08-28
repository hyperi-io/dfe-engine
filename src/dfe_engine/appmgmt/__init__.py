#  Project:      dfe-engine
#  File:         appmgmt/__init__.py
#  Purpose:      Generic management surface for every deployed DFE app
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One management layer for every deployed DFE app.

An app instance is addressed as ``(service, instance)`` and IS its values overlay in
the deploy repo. Five capabilities hang off that one identity: its helm values, the
config contract it declares, its scaling dials, the files it consumes, and its
operational state.

Writes go through the same gitcrud ``helmvars`` path as ``api/v1/helm.py``, so RBAC,
protected-var policy, optimistic concurrency, review routing and audit apply here
without being reimplemented.
"""

from .catalogue import (
    APP_CATALOGUE,
    AppDescriptor,
    ConsumedFileSet,
    ReloadMode,
    UnknownAppError,
    descriptor,
    file_set,
    services,
)
from .files import AppFile, FileNotInSetError, InvalidFilenameError
from .instances import (
    HELMVARS_CLASS,
    AppInstance,
    InstanceExistsError,
    InvalidInstanceError,
    initial_overlay,
    instance_of,
    list_instances,
    parse_overlay_name,
)
from .operations import AppMetrics, AppStatus, MetricsUnavailableError, OperationalReader
from .scaling import DeployTarget, InvalidDialError, ScalingDials

__all__ = [
    "APP_CATALOGUE",
    "HELMVARS_CLASS",
    "AppDescriptor",
    "AppFile",
    "AppInstance",
    "AppMetrics",
    "AppStatus",
    "ConsumedFileSet",
    "DeployTarget",
    "FileNotInSetError",
    "InstanceExistsError",
    "InvalidDialError",
    "InvalidFilenameError",
    "InvalidInstanceError",
    "MetricsUnavailableError",
    "OperationalReader",
    "ReloadMode",
    "ScalingDials",
    "UnknownAppError",
    "descriptor",
    "file_set",
    "initial_overlay",
    "instance_of",
    "list_instances",
    "parse_overlay_name",
    "services",
]
