#  Project:      dfe-engine
#  File:         tests/governance/test_ch_rbac_secret.py
#  Purpose:      Minted CH group password -> secrets seam; only the hash -> gitops
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A group's CH password goes to scalo.secrets; the gitops DDL carries only the hash."""

from __future__ import annotations

import hashlib

from dfe_engine.governance.ch_rbac import GroupChBinding, mint_group_secret
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings


def test_mint_writes_plaintext_to_secrets_and_hash_to_ddl(tmp_path):
    sec = build_secrets(SecretsSettings(provider="file", path=str(tmp_path)))
    binding = GroupChBinding(group="soc-ro", grants=["SELECT ON dfe.*"])

    path, sql = mint_group_secret(binding, sec)

    stored = sec.get("ch/groups/soc-ro")
    assert path == "ddl/ch-rbac/soc-ro.sql"
    # the DDL carries the sha256 hash of exactly the stored plaintext...
    assert hashlib.sha256(stored.encode()).hexdigest() in sql
    # ...and NEVER the plaintext itself.
    assert stored not in sql
    assert "CREATE USER" in sql
    assert "`dfe_grp_soc-ro`" in sql  # backtick-quoted (hyphen-safe)
