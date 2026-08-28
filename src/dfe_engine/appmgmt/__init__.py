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

Alongside them sits the artefact library: a versioned store of authored files that
an instance's file set can be linked to instead of authored into directly. The
library is generic over what a file IS - its kinds are manifest data.

Writes go through the same gitcrud ``helmvars`` path as ``api/v1/helm.py``, so RBAC,
protected-var policy, optimistic concurrency, review routing and audit apply here
without being reimplemented.
"""

from .catalogue import (
    APP_CATALOGUE,
    ARTIFACT_KINDS,
    AppDescriptor,
    ArtifactKind,
    ConsumedFileSet,
    Encoding,
    Multiplicity,
    ReloadMode,
    UnknownAppError,
    descriptor,
    file_set,
    services,
)
from .files import AppFile, FileNotInSetError, InvalidContentError, InvalidFilenameError
from .instances import (
    HELMVARS_CLASS,
    AppInstance,
    InstanceExistsError,
    InvalidInstanceError,
    additional_instance_allowed,
    initial_overlay,
    instance_of,
    list_instances,
    parse_overlay_name,
)
from .library import (
    ArtifactSummary,
    ArtifactVersion,
    InvalidArtifactError,
    LifecycleState,
    TagNotFoundError,
    UnknownKindError,
    VersionNotFoundError,
)
from .links import ArtifactNotLinkableError, Link, LinkNotFoundError, LinkStatus, Usage
from .operations import AppMetrics, AppStatus, MetricsUnavailableError, OperationalReader
from .scaling import DeployTarget, InvalidDialError, ScalingDials
from .validation import ValidationResult, ValidationStatus, validate, validate_language

__all__ = [
    "APP_CATALOGUE",
    "ARTIFACT_KINDS",
    "HELMVARS_CLASS",
    "AppDescriptor",
    "AppFile",
    "AppInstance",
    "AppMetrics",
    "AppStatus",
    "ArtifactKind",
    "ArtifactNotLinkableError",
    "ArtifactSummary",
    "ArtifactVersion",
    "ConsumedFileSet",
    "DeployTarget",
    "Encoding",
    "FileNotInSetError",
    "InstanceExistsError",
    "InvalidArtifactError",
    "InvalidContentError",
    "InvalidDialError",
    "InvalidFilenameError",
    "InvalidInstanceError",
    "LifecycleState",
    "Link",
    "LinkNotFoundError",
    "LinkStatus",
    "MetricsUnavailableError",
    "Multiplicity",
    "OperationalReader",
    "ReloadMode",
    "ScalingDials",
    "TagNotFoundError",
    "UnknownAppError",
    "UnknownKindError",
    "Usage",
    "ValidationResult",
    "ValidationStatus",
    "VersionNotFoundError",
    "additional_instance_allowed",
    "descriptor",
    "file_set",
    "initial_overlay",
    "instance_of",
    "list_instances",
    "parse_overlay_name",
    "services",
    "validate",
    "validate_language",
]
