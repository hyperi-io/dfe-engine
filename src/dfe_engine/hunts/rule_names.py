"""Rule names: what a hunt may reference, and the file each one names.

A rule name becomes a file under the rules directory, so it carries the gitcrud
resource-name contract, and every path built from one must resolve inside that
directory.
"""

from pathlib import Path

from dfe_engine.gitcrud.commit_policy import CommitPolicyError, validate_name


class RuleNameError(ValueError):
    """A rule name that is malformed or resolves outside the rules directory."""


def validate_rule_name(name: str) -> str:
    """Return ``name`` when it is a valid rule name.

    Args:
        name: The rule name as a hunt references it.

    Returns:
        The name unchanged.

    Raises:
        RuleNameError: The name is empty, contains ``..``, or has any character
            outside letters, digits, ``.``, ``_`` and ``-``.
    """
    try:
        validate_name(name)
    except CommitPolicyError as exc:
        raise RuleNameError(
            f"rule name {name!r} must be letters, digits, '.', '_' or '-', without '..'"
        ) from exc
    return name


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
    validate_rule_name(name)
    base = Path(directory)
    path = base / f"{name}{suffix}"
    if not path.resolve().is_relative_to(base.resolve()):
        raise RuleNameError(f"rule name {name!r} resolves outside the rules directory")
    return path
