"""Export RBAC scope constants as TypeScript from the scope catalogue.

Run after any scope catalog changes::

    python rbac-scopes-spec/generate.py

Output is written to ``rbac-scopes-spec/scopes/index.ts`` for review and is
copied into ``dfe-ui`` ``packages/dfe-engine-types/scopes/`` by CI.

Source of truth: the ``scopes_dict`` literal in ``dfe_engine.auth.rbac_scopes``.
This used to be 26 per-category ``*_scopes`` dicts in a sibling
``scope_constants`` module; they were merged into one flat dict, so this reads
the single ``scopes_dict`` now. Parsed with the AST (no import side effects).
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCOPES_MODULE = REPO_ROOT / "src/dfe_engine/auth/rbac_scopes/__init__.py"
SCOPES_VAR = "scopes_dict"
DEFAULT_OUT_DIR = Path(__file__).parent / "scopes"


def _literal(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict):
        keys = [_literal(k) for k in node.keys]
        values = [_literal(v) for v in node.values]
        if any(k is Ellipsis for k in keys):
            raise ValueError("unsupported dict key in scopes_dict")
        return dict(zip(keys, values, strict=True))
    raise ValueError(f"unsupported AST node: {type(node).__name__}")


def _parse_scopes(source_path: Path) -> dict[str, str]:
    tree = ast.parse(source_path.read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if node.targets[0].id != SCOPES_VAR:
            continue
        value = _literal(node.value)
        if not isinstance(value, dict):
            raise ValueError(f"{SCOPES_VAR} must be a dict literal")
        return value
    raise ValueError(f"no {SCOPES_VAR} dict found in {source_path}")


def _render_ts(scopes: dict[str, str]) -> str:
    lines = [
        "// Auto-generated from dfe-engine rbac_scopes (scopes_dict). Do not edit.",
        "",
        "export const scopes = {",
    ]
    for key, val in scopes.items():
        lines.append(f'  {key}: "{val}",')
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
    scopes = _parse_scopes(SCOPES_MODULE)
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "index.ts"
    out_file.write_text(_render_ts(scopes))
    print(f"RBAC scope constants written to {out_file} ({len(scopes)} scopes)")


if __name__ == "__main__":
    main()
