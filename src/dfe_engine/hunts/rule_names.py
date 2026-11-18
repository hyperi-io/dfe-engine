"""Rule files: the file a rule name points at, refused when it leaves the rules directory.

A rule is created, and newly referenced by a hunt, under
:data:`dfe_engine.names.DETECTION_NAME`. A rule file already on disk resolves under the
wider gitcrud resource-name contract instead, so a hand-written file with a ``.`` in its
name keeps running. Every path built from a name must resolve inside the rules directory.
"""

from collections.abc import Iterable
from pathlib import Path

from dfe_engine.gitcrud.commit_policy import CommitPolicyError, validate_name


class RuleNameError(ValueError):
    """A rule name that is malformed or resolves outside the rules directory."""


def rule_file(directory: str | Path, name: str, suffix: str) -> Path:
    """The file ``name`` names under ``directory``, refused when it leaves it.

    Args:
        directory: The rules directory.
        name: The rule name.
        suffix: The file extension, dot included.

    Returns:
        The unresolved path, so log lines show the configured directory.

    Raises:
        RuleNameError: The name is invalid, or the path resolves outside the
            directory, through a symlink included.
    """
    try:
        validate_name(name)
    except CommitPolicyError as exc:
        raise RuleNameError(
            f"rule name {name!r} must be letters, digits, '.', '_' or '-', without '..'"
        ) from exc
    base = Path(directory)
    path = base / f"{name}{suffix}"
    if not path.resolve().is_relative_to(base.resolve()):
        raise RuleNameError(f"rule name {name!r} resolves outside the rules directory")
    return path


def missing_rules(directory: str | Path, names: Iterable[str]) -> list[str]:
    """The rule names with no ``{name}.yaml`` under ``directory``, each once, in order.

    A name no file can carry counts as missing, since the runner cannot read it either.

    Args:
        directory: The rules directory.
        names: The rule names a hunt carries.

    Returns:
        The names without a rule file.
    """
    missing: list[str] = []
    for name in dict.fromkeys(names):
        try:
            present = rule_file(directory, name, ".yaml").is_file()
        except RuleNameError:
            present = False
        if not present:
            missing.append(name)
    return missing
