#!/usr/bin/env python3
#  Project:      dfe-engine
#  File:         openapi-spec/route_map.py
#  Purpose:      Generate the API route map: where each operation moves in the resource hierarchy
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

r"""Write ``docs/reference/api-route-map.md``: where each API operation goes in the resource hierarchy.

The page joins three inputs and is never edited by hand:

- the app's own spec, so every operation the engine serves has exactly one row;
- ``route-plan.toml``, where each operation goes and why;
- ``route-consumers.json``, how many files in each consumer repo name each path.

A row's operation id, CLI command and MCP tool all come from its new path, parsed
by the ``HIERARCHY`` the app names its own operations with. A new path that breaks
a hierarchy rule, an operation with no row and a row for no operation all stop the
run, so the page cannot drift from the code.

Usage::

    uv run python openapi-spec/generate.py
    uv run python openapi-spec/route_map.py --refresh-consumers \
        --repo dfe-ui=/projects/dfe-ui --repo dfe-infra=/projects/dfe-infra \
        --repo dfe-docker=/projects/dfe-docker

The first regenerates the spec and this page. The second searches each consumer at
``--ref`` (default ``origin/main``) and this repo's CLI at HEAD, rewrites
``route-consumers.json``, then the page.
"""

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scalo.api import OperationName

from dfe_engine.api.conventions import HIERARCHY

SPEC_DIR = Path(__file__).resolve().parent
REPO_ROOT = SPEC_DIR.parent
PLAN_FILE = SPEC_DIR / "route-plan.toml"
CONSUMERS_FILE = SPEC_DIR / "route-consumers.json"
MAP_FILE = REPO_ROOT / "docs" / "reference" / "api-route-map.md"

STATUSES = ("moved", "merged", "unchanged", "frozen", "decide", "removed")
STATUS_LABELS = {"merged": "merged into"}
# A row in these states names where it goes, so its target is parsed and checked.
PLACED = frozenset({"moved", "merged", "unchanged"})
METHODS = frozenset({"GET", "PUT", "PATCH", "POST", "DELETE"})

# Each consumer: its label in the page, the paths searched, and paths skipped as generated from the spec.
CONSUMERS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "dfe-ui": (
        "ui",
        ("apps", "packages"),
        ("packages/dfe-engine-types/specs", "packages/dfe-engine-types/types"),
    ),
    "dfe-infra": ("infra", ("scripts",), ()),
    "dfe-docker": ("docker", (".",), ()),
    "dfe-engine": ("cli", ("src/dfe_engine/cli",), ()),
}
SELF = "dfe-engine"

_SEGMENT = r"(?:\$\{[^}/]*\}|\{[^}/]*\}|%[sd]|[A-Za-z0-9_-])+"
_CANDIDATE = re.compile(rf"/api/v1((?:/{_SEGMENT})*)(/?)")
_PARAM = re.compile(r"\{[^}]*\}")


class RouteMapError(ValueError):
    """The plan disagrees with the app, or proposes a path the hierarchy refuses."""


@dataclass(frozen=True, slots=True)
class Row:
    """One operation the engine serves today, and where it goes."""

    method: str
    path: str
    status: str
    new_method: str
    new_path: str
    verb: str | None
    note: str | None
    hidden: bool

    @property
    def key(self) -> str:
        """``"<METHOD> <path>"`` the engine serves today."""
        return f"{self.method} {self.path}"

    @property
    def target(self) -> str:
        """``"<METHOD> <path>"`` it moves to, the same as :attr:`key` when it stays."""
        return f"{self.new_method} {self.new_path}"


def _split(operation: str, where: str) -> tuple[str, str]:
    method, _, path = operation.partition(" ")
    if method not in METHODS or not path.startswith("/"):
        raise RouteMapError(f"{where}: {operation!r} is not '<METHOD> <path>'")
    return method, path


def load_plan(path: Path = PLAN_FILE) -> tuple[list[Row], dict[str, str]]:
    """The plan's rows and notes, each row checked for the fields its status needs.

    Raises:
        RouteMapError: Naming every malformed row.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    notes: dict[str, str] = data.get("notes", {})
    rows: list[Row] = []
    errors: list[str] = []
    for entry in data.get("routes", []):
        try:
            method, old = _split(entry["old"], "old")
            status = entry["status"]
            if status not in STATUSES:
                raise RouteMapError(f"{entry['old']}: unknown status {status!r}")
            new = entry.get("new")
            if status in {"moved", "merged"} and not new:
                raise RouteMapError(f"{entry['old']}: a {status} row names its new path")
            if status not in {"moved", "merged"} and new:
                raise RouteMapError(f"{entry['old']}: a {status} row names no new path")
            new_method, new_path = _split(new, "new") if new else (method, old)
            note = entry.get("note")
            if status in {"decide", "frozen"} and not note:
                raise RouteMapError(f"{entry['old']}: a {status} row names its note")
            if note and note not in notes:
                raise RouteMapError(f"{entry['old']}: no note {note!r} in [notes]")
            rows.append(
                Row(
                    method,
                    old,
                    status,
                    new_method,
                    new_path,
                    entry.get("verb"),
                    note,
                    entry.get("hidden", False),
                )
            )
        except (KeyError, RouteMapError) as exc:
            errors.append(str(exc) if isinstance(exc, RouteMapError) else f"{entry}: missing {exc}")
    unused = sorted(set(notes) - {row.note for row in rows})
    errors.extend(f"note {name!r} is used by no row" for name in unused)
    if errors:
        raise RouteMapError("\n".join(errors))
    return rows, notes


def _target_name(row: Row) -> OperationName:
    return HIERARCHY.parse(f"{row.new_path}", row.new_method, verb=row.verb)


def check_plan(rows: list[Row], operations: set[str]) -> None:
    """Hold the plan to the app's operations and to the hierarchy.

    Args:
        rows: The plan's rows.
        operations: ``"<METHOD> <path>"`` for each operation the app serves.

    Raises:
        RouteMapError: Naming every operation with no row, row for no operation,
            new path the hierarchy refuses, shared target and dangling merge.
    """
    errors: list[str] = []
    seen = Counter(row.key for row in rows)
    errors.extend(f"{key}: {count} rows" for key, count in sorted(seen.items()) if count > 1)
    placed = {row.target for row in rows if row.status in {"moved", "unchanged"}}
    for operation in sorted(operations):
        if operation not in seen and operation not in placed:
            errors.append(f"{operation}: served, but the plan has no row for it")
    for row in rows:
        if row.key not in operations and not (row.status in PLACED and row.target in operations):
            errors.append(f"{row.key}: neither it nor its new path is served")
    targets: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if row.status not in PLACED:
            continue
        name = _target_name(row)
        problems = list(name.problems)
        if name.depth > HIERARCHY.max_depth:
            problems.append(f"{name.depth} levels, over {HIERARCHY.max_depth}")
        if problems:
            errors.append(f"{row.key} -> {row.target}: {'; '.join(problems)}")
        if row.status != "merged":
            targets[name.operation_id].append(row.key)
    errors.extend(
        f"{oid}: the target of {', '.join(keys)}"
        for oid, keys in sorted(targets.items())
        if len(keys) > 1
    )
    for row in rows:
        if row.status == "merged" and row.target not in placed:
            errors.append(
                f"{row.key}: merged into {row.target}, which no moved or unchanged row produces"
            )
    if errors:
        raise RouteMapError("\n".join(errors))


def template_parts(path: str) -> list[str]:
    """A route template's segments below ``/api/v1``."""
    return path.removeprefix("/api/v1").strip("/").split("/")


def _resolve(segments: list[str], templates: list[tuple[str, list[str]]]) -> str | None:
    """The most specific template a consumer's path matches: literal segments beat parameters."""
    best, best_score = None, -1
    for template, parts in templates:
        if len(parts) != len(segments):
            continue
        score = 0
        for want, got in zip(parts, segments, strict=True):
            if want.startswith("{"):
                continue
            if "{" in got or "%" in got or want != got:
                break
            score += 1
        else:
            if score > best_score:
                best, best_score = template, score
    return best


def paths_named(text: str, templates: list[tuple[str, list[str]]]) -> tuple[set[str], set[str]]:
    """The engine paths a file names, and the ``/api/v1`` paths it names that match none.

    A segment written as a template expression (``${name}``, ``{name}``, ``%s``)
    matches a path parameter, and a trailing slash stands for one.
    """
    found: set[str] = set()
    unmatched: set[str] = set()
    for match in _CANDIDATE.finditer(text):
        segments = [segment for segment in match.group(1).split("/") if segment]
        if match.group(2):
            segments.append("{}")
        if not segments:
            continue
        template = _resolve(segments, templates)
        if template is None:
            unmatched.add(match.group(0))
        else:
            found.add(template)
    return found, unmatched


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode not in (0, 1):
        raise RouteMapError(f"git {' '.join(args)} in {repo}: {result.stderr.strip()}")
    return result.stdout


def scan_consumer(
    repo: Path, ref: str, name: str, templates: list[tuple[str, list[str]]]
) -> tuple[str, Counter[str], list[str]]:
    """Search one consumer at ``ref``: its commit, files naming each path, and unmatched paths."""
    _, include, exclude = CONSUMERS[name]
    commit = _git(repo, "rev-parse", ref).strip()
    pathspecs = [*include, *(f":(exclude){path}" for path in exclude)]
    listed = _git(repo, "grep", "-l", "-I", "-F", "/api/v1/", commit, "--", *pathspecs)
    counts: Counter[str] = Counter()
    unmatched: set[str] = set()
    for line in listed.splitlines():
        path = line.split(":", 1)[1]
        found, missed = paths_named(_git(repo, "show", f"{commit}:{path}"), templates)
        counts.update(found)
        unmatched |= missed
    return commit, counts, sorted(unmatched)


def refresh_consumers(spec: dict[str, Any], repos: dict[str, Path], ref: str) -> dict[str, Any]:
    """Search every consumer and write ``route-consumers.json``."""
    templates = [
        (path, template_parts(path)) for path in spec["paths"] if path.startswith("/api/v1/")
    ]
    snapshot: dict[str, Any] = {"repos": {}, "counts": defaultdict(dict), "unmatched": {}}
    for name in CONSUMERS:
        repo, at = (REPO_ROOT, "HEAD") if name == SELF else (repos[name], ref)
        commit, counts, unmatched = scan_consumer(repo, at, name, templates)
        snapshot["repos"][name] = {"ref": at, "commit": commit}
        for path, files in counts.items():
            snapshot["counts"][path][name] = files
        snapshot["unmatched"][name] = unmatched
    snapshot["counts"] = {
        path: dict(sorted(by.items())) for path, by in sorted(snapshot["counts"].items())
    }
    CONSUMERS_FILE.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8", newline="\n")
    return snapshot


def _code(text: str) -> str:
    return f"`{text}`"


def _consumers(path: str, counts: dict[str, dict[str, int]]) -> str:
    by = counts.get(path, {})
    cells = [f"{CONSUMERS[name][0]} {by[name]}" for name in CONSUMERS if by.get(name)]
    return ", ".join(cells) or "0"


def _renamed_only(row: Row) -> bool:
    same_shape = _PARAM.sub("{}", row.path) == _PARAM.sub("{}", row.new_path)
    return (
        row.status == "moved"
        and row.method == row.new_method
        and same_shape
        and row.path != row.new_path
    )


def _table(rows: list[Row], cells: dict[str, list[str]]) -> list[str]:
    lines = [
        "| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in sorted(rows, key=lambda row: (row.path, row.method)):
        lines.append("| " + " | ".join(cells[row.key]) + " |")
    return lines


def render(
    spec: dict[str, Any], rows: list[Row], notes: dict[str, str], consumers: dict[str, Any]
) -> str:
    """The route map page for ``spec`` under the plan, deterministic for the same inputs."""
    operations = {
        f"{m.upper()} {p}": op for p, item in spec["paths"].items() for m, op in item.items()
    }
    counts = consumers.get("counts", {})
    cells: dict[str, list[str]] = {}
    for row in rows:
        old = _code(row.key)
        used = _consumers(row.path, counts)
        if row.status in PLACED:
            name = _target_name(row)
            note = notes.get(row.note or "", "")
            if not note and _renamed_only(row):
                note = "Same URL: the path parameter is renamed."
            cells[row.key] = [
                old,
                _code(row.target),
                _code(name.operation_id),
                _code(f"dfe {' '.join(name.hierarchy)} {name.verb}"),
                _code(HIERARCHY.tool_name(name)),
                STATUS_LABELS.get(row.status, row.status),
                used,
                note,
            ]
        elif row.status == "frozen":
            current = operations.get(row.key, {}).get("operationId", "-")
            name = HIERARCHY.parse(row.path, row.method)
            cli = "-" if row.hidden else _code(f"dfe {' '.join(name.hierarchy)} {name.verb}")
            cells[row.key] = [old, old, _code(current), cli, "-", "frozen", used, ""]
        else:
            cells[row.key] = [old, "-", "-", "-", "-", row.status, used, ""]

    by_status = Counter(row.status for row in rows)
    placed_roots: dict[str, list[Row]] = defaultdict(list)
    grouped: dict[str, dict[str, list[Row]]] = {
        "frozen": defaultdict(list),
        "decide": defaultdict(list),
    }
    for row in rows:
        if row.status in PLACED:
            placed_roots[_target_name(row).group].append(row)
        elif row.status in grouped:
            grouped[row.status][row.note or ""].append(row)
    removed = [row for row in rows if row.status == "removed"]

    repos = consumers.get("repos", {})
    sources = ", ".join(
        f"{CONSUMERS[name][0]} = {name} {repos[name]['commit'][:12]}"
        for name in CONSUMERS
        if name in repos
    )
    lines = [
        "# API route map",
        "",
        "Where each engine API operation goes in the resource hierarchy. "
        "`openapi-spec/route_map.py` generates this page from the engine's routes, "
        "`openapi-spec/route-plan.toml` and `openapi-spec/route-consumers.json`: "
        "change those and run `uv run python openapi-spec/generate.py`, never this page.",
        "",
        "One row per operation the engine serves today. **New** is where it goes, and its operation id, "
        "`dfe` CLI command and MCP tool all come from that path. "
        "An MCP tool exists only for an operation that opts in. "
        "**Consumers** counts the files in each repo that name the old path, whatever the method "
        f"({sources or 'not searched yet'}).",
        "",
        "## Summary",
        "",
        "| Status | Operations | Meaning |",
        "|---|---|---|",
    ]
    meanings = {
        "moved": "A new path, method or both",
        "merged": "Folded into the operation **New** names",
        "unchanged": "Same method and path",
        "frozen": "Kept as it is",
        "decide": "Not placed yet: see its question",
        "removed": "Gone, with nothing in its place",
    }
    for status in STATUSES:
        lines.append(
            f"| {STATUS_LABELS.get(status, status)} | {by_status[status]} | {meanings[status]} |"
        )
    lines += ["| total | " + str(len(rows)) + " | |", ""]

    questions = sorted(grouped["decide"].items(), key=lambda item: (-len(item[1]), item[0]))
    if questions:
        lines += ["## Questions", "", "| Question | Operations |", "|---|---|"]
        lines += [f"| [{key}](#{key}) | {len(members)} |" for key, members in questions]
        lines.append("")

    for root in sorted(placed_roots):
        lines += [f"## {root}", "", *_table(placed_roots[root], cells), ""]
    for note, members in sorted(grouped["frozen"].items()):
        lines += [f"## Frozen: {note}", "", notes[note], "", *_table(members, cells), ""]
    if removed:
        lines += ["## Removed", "", *_table(removed, cells), ""]
    if questions:
        lines += ["## To decide", ""]
        for key, members in questions:
            lines += [f"### {key}", "", notes[key], "", *_table(members, cells), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def build(spec: dict[str, Any]) -> str:
    """The page for ``spec`` from the committed plan and consumer counts.

    Raises:
        RouteMapError: When the plan disagrees with the spec or the hierarchy.
    """
    rows, notes = load_plan()
    operations = {f"{m.upper()} {p}" for p, item in spec["paths"].items() for m in item}
    check_plan(rows, operations)
    consumers = (
        json.loads(CONSUMERS_FILE.read_text(encoding="utf-8")) if CONSUMERS_FILE.exists() else {}
    )
    return render(spec, rows, notes, consumers)


def write(spec: dict[str, Any]) -> Path:
    """Write the page for ``spec`` and return its path."""
    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    MAP_FILE.write_text(build(spec), encoding="utf-8", newline="\n")
    return MAP_FILE


def _spec() -> dict[str, Any]:
    sys.path.insert(0, str(SPEC_DIR))
    from generate import product_spec

    return product_spec()


def main(argv: list[str] | None = None) -> int:
    """Regenerate the page, refreshing the consumer counts first when asked."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument(
        "--refresh-consumers",
        action="store_true",
        help="search each consumer repo and rewrite route-consumers.json",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help=f"a consumer checkout, for each of {', '.join(n for n in CONSUMERS if n != SELF)}",
    )
    parser.add_argument(
        "--ref",
        default="origin/main",
        help="the ref searched in each consumer (default origin/main)",
    )
    args = parser.parse_args(argv)

    spec = _spec()
    if args.refresh_consumers:
        repos = dict(item.split("=", 1) for item in args.repo)
        missing = [name for name in CONSUMERS if name != SELF and name not in repos]
        if missing:
            parser.error(f"--refresh-consumers needs --repo for {', '.join(missing)}")
        snapshot = refresh_consumers(
            spec, {name: Path(path) for name, path in repos.items()}, args.ref
        )
        unmatched = {name: len(paths) for name, paths in snapshot["unmatched"].items()}
        print(f"Consumer counts written to {CONSUMERS_FILE} (unmatched /api/v1 paths: {unmatched})")
    try:
        path = write(spec)
    except RouteMapError as exc:
        print(f"route map refused:\n{exc}", file=sys.stderr)
        return 1
    print(f"Route map written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
