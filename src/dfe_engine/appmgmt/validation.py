#  Project:      dfe-engine
#  File:         appmgmt/validation.py
#  Purpose:      Syntax validation for authored transform files
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Check an authored transform before it reaches the deploy repo.

dfe-transform-vrl compiles every VRL program at startup and hard-fails on one it
cannot compile, so an editor with no syntax check writes a crashloop into git.

The VRL backend is ``vectordotdev``, which is imported INSIDE the call and never at
module scope: it links against compiled Vector artifacts that a deployment need not
have, and dfe-engine has to start without it. An absent or broken backend reports
``unavailable`` rather than raising, so validation can never be the reason a save
fails when the checker itself is the thing that is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from dfe_engine.yaml_utils import YAMLError, yaml_load_string

from .catalogue import ConsumedFileSet


class ValidationStatus(StrEnum):
    """Outcome of a validation attempt."""

    VALID = "valid"
    INVALID = "invalid"

    UNAVAILABLE = "unavailable"
    """No backend could answer; the content was not checked."""

    DISABLED = "disabled"
    """Validation is switched off for this deployment."""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """What a validator concluded about one file."""

    status: ValidationStatus
    backend: str = ""
    message: str = ""
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blocks_write(self) -> bool:
        """Whether this outcome should stop the file being committed."""
        return self.status is ValidationStatus.INVALID


def _validate_vrl(content: str) -> ValidationResult:
    """Compile-check a VRL program through vectordotdev, if it is installed."""
    try:
        from vectordotdev import vrl_check  # ty: ignore[unresolved-import]
    except ImportError as exc:
        return ValidationResult(
            status=ValidationStatus.UNAVAILABLE,
            backend="vectordotdev",
            message=f"vectordotdev is not installed: {exc}",
        )

    try:
        outcome = vrl_check(content)
    except ImportError as exc:
        # vectordotdev ships stubs that raise ImportError when its compiled
        # bindings are absent, so the package importing is not proof it can run.
        return ValidationResult(
            status=ValidationStatus.UNAVAILABLE,
            backend="vectordotdev",
            message=f"vectordotdev bindings are unavailable: {exc}",
        )
    except Exception as exc:
        return ValidationResult(
            status=ValidationStatus.UNAVAILABLE,
            backend="vectordotdev",
            message=f"vectordotdev failed to run: {exc}",
        )

    return _interpret_vrl(outcome)


def _interpret_vrl(outcome: object) -> ValidationResult:
    """Map vectordotdev's answer onto a result without assuming its exact shape.

    The binding is being reworked, so accept a bool, a (ok, errors) pair or a
    mapping, and report ``unavailable`` for anything else rather than guessing.
    """
    backend = "vectordotdev"
    if isinstance(outcome, bool):
        return ValidationResult(
            status=ValidationStatus.VALID if outcome else ValidationStatus.INVALID,
            backend=backend,
            message="" if outcome else "VRL did not compile",
        )
    if isinstance(outcome, dict):
        ok = bool(outcome.get("ok", outcome.get("valid", False)))
        raw = outcome.get("errors") or ([] if ok else [str(outcome.get("error", ""))])
        errors = tuple(str(e) for e in raw if str(e))
        return ValidationResult(
            status=ValidationStatus.VALID if ok else ValidationStatus.INVALID,
            backend=backend,
            message="" if ok else "; ".join(errors) or "VRL did not compile",
            errors=errors,
        )
    if isinstance(outcome, tuple) and len(outcome) == 2:
        ok, raw = outcome
        errors = tuple(str(e) for e in (raw or ()) if str(e))
        return ValidationResult(
            status=ValidationStatus.VALID if ok else ValidationStatus.INVALID,
            backend=backend,
            message="" if ok else "; ".join(errors) or "VRL did not compile",
            errors=errors,
        )
    return ValidationResult(
        status=ValidationStatus.UNAVAILABLE,
        backend=backend,
        message=f"unrecognised vectordotdev result: {type(outcome).__name__}",
    )


def _validate_vector_yaml(content: str) -> ValidationResult:
    """Check a Vector transform fragment parses and declares a transform type.

    Only the shape this layer can be sure of without Vector itself: a fragment
    missing `type` is rejected by `vector validate` at assembly time, which the
    supervisor turns into a rolled-back reload rather than a visible error.
    """
    try:
        parsed = yaml_load_string(content)
    except YAMLError as exc:
        return ValidationResult(
            status=ValidationStatus.INVALID,
            backend="yaml",
            message=f"not valid YAML: {exc}",
            errors=(str(exc),),
        )
    if not isinstance(parsed, dict):
        return ValidationResult(
            status=ValidationStatus.INVALID,
            backend="yaml",
            message="a Vector transform must be a mapping",
        )
    if "type" not in parsed:
        return ValidationResult(
            status=ValidationStatus.INVALID,
            backend="yaml",
            message="a Vector transform must declare a 'type'",
        )
    return ValidationResult(status=ValidationStatus.VALID, backend="yaml")


_BACKENDS = {
    "vrl": _validate_vrl,
    "yaml": _validate_vector_yaml,
}


def validate_language(language: str, content: str, *, enabled: bool) -> ValidationResult:
    """Validate content written in a language.

    ``enabled`` is the deployment's switch. Off returns ``disabled`` rather than
    silently passing, so a caller can tell "checked and fine" from "not checked".
    A language with no registered backend reports ``unavailable``.
    """
    if not enabled:
        return ValidationResult(status=ValidationStatus.DISABLED)
    backend = _BACKENDS.get(language)
    if backend is None:
        return ValidationResult(
            status=ValidationStatus.UNAVAILABLE,
            message=f"no validator for {language!r}",
        )
    return backend(content)


def validate(file_set: ConsumedFileSet, content: str, *, enabled: bool) -> ValidationResult:
    """Validate content for the app that will consume it."""
    return validate_language(file_set.language, content, enabled=enabled)
