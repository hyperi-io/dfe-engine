"""Export RBAC scope constants as TypeScript from ``scope_constants.py``.

Run after any scope catalog changes::

    python rbac-scopes-spec/generate.py

Output is written to ``rbac-scopes-spec/scopes/index.ts`` for review and is
copied into ``dfe-ui`` ``packages/dfe-engine-types/scopes/`` by CI.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCOPE_CONSTANTS = REPO_ROOT / "src/dfe_engine/auth/rbac_scopes/scope_constants.py"
DEFAULT_OUT_DIR = Path(__file__).parent / "scopes"


def _literal(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict):
        keys = [_literal(k) for k in node.keys]
        values = [_literal(v) for v in node.values]
        if any(k is Ellipsis for k in keys):
            raise ValueError("unsupported dict key in scope_constants.py")
        return dict(zip(keys, values, strict=True))
    raise ValueError(f"unsupported AST node: {type(node).__name__}")


def _parse_scope_groups(source_path: Path) -> list[tuple[str, dict[str, str]]]:
    tree = ast.parse(source_path.read_text())
    groups: list[tuple[str, dict[str, str]]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        name = node.targets[0].id
        if not name.endswith("_scopes"):
            continue
        value = _literal(node.value)
        if not isinstance(value, dict):
            raise ValueError(f"{name} must be a dict literal")
        groups.append((name, value))
    if not groups:
        raise ValueError(f"no *_scopes dicts found in {source_path}")
    return groups


def _render_ts(groups: list[tuple[str, dict[str, str]]]) -> str:
    lines = [
        "// Auto-generated from dfe-engine scope_constants.py. Do not edit.",
        "",
    ]
    for name, scopes in groups:
        lines.append(f"const {name} = {{")
        for key, val in scopes.items():
            lines.append(f'  {key}: "{val}",')
        lines.append("} as const;")
        lines.append("")

    lines.append("export const scopes = {")
    for name, _ in groups:
        lines.append(f"  ...{name},")
    lines.append("} as const;")
    lines.append("")
    lines.append("export type RbacScope = (typeof scopes)[keyof typeof scopes];")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Directory for index.ts (default: {DEFAULT_OUT_DIR})",
    )
    args = parser.parse_args()
    groups = _parse_scope_groups(SCOPE_CONSTANTS)
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "index.ts"
    out_file.write_text(_render_ts(groups))
    print(f"RBAC scope constants written to {out_file} ({len(groups)} groups)")


if __name__ == "__main__":
    main()
