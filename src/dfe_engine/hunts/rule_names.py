"""Rule names: what a rule may be called, and the file each one names.

A rule is created, and a hunt references it, under letters, digits, ``_`` and ``-``.
That is the charset of every rule id the engine generates (sigma bindings, HyperDX
views). A rule file already on disk resolves under the wider gitcrud resource-name
contract instead, so a hand-written file with a ``.`` in its name keeps running. Every
path built from a name must resolve inside the rules directory.
"""

import re
from pathlib import Path

from dfe_engine.gitcrud.commit_policy import CommitPolicyError, validate_name

MAX_RULE_NAME_LENGTH = 128
"""The longest name a rule may have: the cap the account and group stores put on theirs."""

RULE_NAME = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_RULE_NAME_LENGTH}}}")
"""What a new rule, or a hunt's reference to one, may be called; matched with ``fullmatch``."""


class RuleNameError(ValueError):
    """A rule name that is malformed or resolves outside the rules directory."""


def validate_rule_name(name: str) -> str:
    """Return ``name`` when a rule can be created, or referenced by a hunt, under it.

    Args:
        name: The rule name as the create endpoint or a hunt receives it.

    Returns:
        The name unchanged.

    Raises:
        RuleNameError: The name is empty, longer than :data:`MAX_RULE_NAME_LENGTH`,
            or has a character outside letters, digits, ``_`` and ``-``.
    """
    if not RULE_NAME.fullmatch(name):
        raise RuleNameError(
            f"rule name {name!r} must be letters, digits, '_' or '-', "
            f"at most {MAX_RULE_NAME_LENGTH} characters"
        )
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
