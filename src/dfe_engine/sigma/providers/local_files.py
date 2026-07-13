#  Project:      dfe-engine
#  File:         sigma/providers/local_files.py
#  Purpose:      Sigma provider: a local directory of *.yml (the default import file)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Local-directory sigma provider - makes the "default import file" just a provider.

The historical one-way file converter scanned an input directory. Modelling that
same directory as a provider means the default import file feeds the SAME id-keyed
gitcrud catalogue as every online provider, through the SAME idempotent upsert -
so a re-import is reliable (add / skip-unchanged / preserve-local-edit), which is
exactly the reliability the operator asked for (spec section G).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from scalo.logger import logger

from .base import ProviderConfig, SigmaProvider, SigmaRuleDoc, modified_since, parse_sigma_yaml


class LocalFilesProvider(SigmaProvider):
    """Scan a local directory of *.yml / *.yaml sigma rules. Provenance origin='file'."""

    def __init__(self, config: ProviderConfig, *, secrets=None):
        super().__init__(config, secrets=secrets)

    @property
    def origin(self) -> str:
        # The default import file is the canonical 'file' origin, not 'provider:<name>'.
        return "file"

    async def fetch(self, since: datetime | None = None) -> list[SigmaRuleDoc]:
        directory = Path(str(self.config.options.get("directory", ""))).expanduser()
        docs: list[SigmaRuleDoc] = []
        warnings: list[str] = []
        if not directory.is_dir():
            logger.warning(
                "sigma local_files provider: directory missing",
                provider=self.name,
                directory=str(directory),
            )
            self.last_warnings = warnings
            return docs
        # De-dup .yml/.yaml (a file could theoretically match both globs is impossible,
        # but sorting a merged set keeps a stable, deterministic scan order).
        paths = sorted({*directory.rglob("*.yml"), *directory.rglob("*.yaml")})
        for path in paths:
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                warnings.append(f"read failed {path}: {exc}")
                continue
            source_ref = str(path.relative_to(directory))
            parsed, warns = parse_sigma_yaml(text, self.origin, source_ref)
            docs.extend(parsed)
            warnings.extend(f"{source_ref}: {w}" for w in warns)
        if since is not None:
            docs = [d for d in docs if modified_since(d, since)]
        self.last_warnings = warnings
        return docs
