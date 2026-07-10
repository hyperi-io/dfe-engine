#  Project:      dfe-engine
#  File:         cli/auto/config.py
#  Purpose:      Named-configuration + credential store under ~/.config/dfe
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Credential + configuration store for the ``dfe`` CLI (aws/gcloud shaped).

Layout under ``~/.config/dfe`` (override the whole root with ``$DFE_CONFIG_HOME``)::

    configurations/config_<name>   INI: [core] url = ..., account = ...
    active_config                  one line: the active profile name
    credentials.json               {account: {token|api_key}}, chmod 0600

Profiles are named configurations (``gcloud config configurations``). The active
profile pointer selects one. Credentials are keyed by ``account`` so several
profiles pointing at the same account share one stored token.

Resolution precedence (first wins):
  url   : explicit --url / $DFE_API_URL  >  active profile url
  token : $DFE_API_KEY / $DFE_API_TOKEN  >  stored credential for the active account
"""

from __future__ import annotations

import configparser
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import DfeConfigError

_DEFAULT_PROFILE = "default"


def config_home() -> Path:
    """Root config dir - ``$DFE_CONFIG_HOME`` or ``~/.config/dfe``."""
    override = os.environ.get("DFE_CONFIG_HOME")
    if override:
        return Path(override)
    return Path.home() / ".config" / "dfe"


@dataclass
class Credential:
    """A resolved credential: exactly one of token / api_key is set."""

    account: str
    token: str | None = None
    api_key: str | None = None

    @property
    def is_api_key(self) -> bool:
        return self.api_key is not None

    @property
    def value(self) -> str | None:
        return self.api_key if self.api_key is not None else self.token


class Store:
    """File-backed accessor for profiles + the credential file."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = home or config_home()

    # -- paths ---------------------------------------------------------------

    @property
    def configurations_dir(self) -> Path:
        return self.home / "configurations"

    @property
    def active_config_file(self) -> Path:
        return self.home / "active_config"

    @property
    def credentials_file(self) -> Path:
        return self.home / "credentials.json"

    def _profile_file(self, name: str) -> Path:
        return self.configurations_dir / f"config_{name}"

    def _ensure_home(self) -> None:
        # Credentials live here - keep the tree owner-only (0700). mkdir's mode is
        # umask-masked, so chmod after to guarantee the bits regardless of umask.
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.configurations_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (self.home, self.configurations_dir):
            try:
                directory.chmod(0o700)
            except OSError:  # best-effort on filesystems that ignore mode bits
                pass

    # -- active profile ------------------------------------------------------

    def active_profile(self) -> str:
        if self.active_config_file.exists():
            name = self.active_config_file.read_text(encoding="utf-8").strip()
            if name:
                return name
        return _DEFAULT_PROFILE

    def set_active_profile(self, name: str) -> None:
        self._ensure_home()
        self.active_config_file.write_text(name + "\n", encoding="utf-8")

    def list_profiles(self) -> list[str]:
        if not self.configurations_dir.exists():
            return []
        names = [
            p.name[len("config_") :]
            for p in self.configurations_dir.glob("config_*")
            if p.is_file()
        ]
        return sorted(names)

    def create_profile(self, name: str) -> None:
        self._ensure_home()
        path = self._profile_file(name)
        if not path.exists():
            parser = configparser.ConfigParser()
            parser["core"] = {}
            with path.open("w", encoding="utf-8") as fh:
                parser.write(fh)

    def delete_profile(self, name: str) -> None:
        path = self._profile_file(name)
        if path.exists():
            path.unlink()
        if self.active_profile() == name:
            self.set_active_profile(_DEFAULT_PROFILE)

    # -- profile key/value ---------------------------------------------------

    def _read_profile(self, name: str) -> configparser.ConfigParser:
        parser = configparser.ConfigParser()
        path = self._profile_file(name)
        if path.exists():
            try:
                parser.read(path, encoding="utf-8")
            except configparser.Error as exc:
                # MissingSectionHeaderError etc. - degrade to a friendly error,
                # consistent with how a corrupt credentials.json is handled.
                raise DfeConfigError(f"corrupt profile config: {exc}") from exc
        if not parser.has_section("core"):
            parser.add_section("core")
        return parser

    def get_value(self, key: str, name: str | None = None) -> str | None:
        parser = self._read_profile(name or self.active_profile())
        return parser["core"].get(key)

    def set_value(self, key: str, value: str, name: str | None = None) -> None:
        self._ensure_home()
        profile = name or self.active_profile()
        parser = self._read_profile(profile)
        parser["core"][key] = value
        with self._profile_file(profile).open("w", encoding="utf-8") as fh:
            parser.write(fh)

    def profile_items(self, name: str | None = None) -> dict[str, str]:
        parser = self._read_profile(name or self.active_profile())
        return dict(parser["core"])

    # -- credentials ---------------------------------------------------------

    def _read_credentials(self) -> dict[str, dict[str, str]]:
        if not self.credentials_file.exists():
            return {}
        try:
            return json.loads(self.credentials_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_credentials(self, creds: dict[str, dict[str, str]]) -> None:
        self._ensure_home()
        payload = json.dumps(creds, indent=2) + "\n"
        # Create-with-mode-0600: the file must never briefly exist world-readable
        # between write and chmod. os.open honours the mode on O_CREAT; on rewrite
        # the existing 0600 file is truncated (O_TRUNC), never widened.
        path = self.credentials_file

        def _opener(p: str, flags: int) -> int:
            return os.open(p, flags, 0o600)

        with open(path, "w", encoding="utf-8", opener=_opener) as fh:
            fh.write(payload)
        try:
            path.chmod(0o600)  # tighten an existing file that predates this code
        except OSError:  # best-effort on filesystems that ignore mode bits
            pass

    def save_credential(self, cred: Credential) -> None:
        creds = self._read_credentials()
        entry: dict[str, str] = {}
        if cred.api_key is not None:
            entry["api_key"] = cred.api_key
        if cred.token is not None:
            entry["token"] = cred.token
        creds[cred.account] = entry
        self._write_credentials(creds)

    def get_credential(self, account: str) -> Credential | None:
        entry = self._read_credentials().get(account)
        if not entry:
            return None
        return Credential(
            account=account,
            token=entry.get("token"),
            api_key=entry.get("api_key"),
        )

    def delete_credential(self, account: str) -> None:
        creds = self._read_credentials()
        if account in creds:
            del creds[account]
            self._write_credentials(creds)

    def list_accounts(self) -> list[str]:
        return sorted(self._read_credentials().keys())


@dataclass
class Resolved:
    """The effective url + credential for one CLI invocation."""

    url: str | None
    credential: Credential | None
    account: str | None
    profile: str


def resolve(
    store: Store,
    *,
    url_override: str | None = None,
    profile_override: str | None = None,
) -> Resolved:
    """Resolve url + credential per the documented precedence."""
    profile = profile_override or store.active_profile()

    # URL: explicit flag / env win over the profile.
    url = url_override or os.environ.get("DFE_API_URL") or store.get_value("url", profile)

    account = store.get_value("account", profile)

    # Token: env wins; else the stored credential for the active account.
    env_key = os.environ.get("DFE_API_KEY")
    env_token = os.environ.get("DFE_API_TOKEN")
    credential: Credential | None = None
    if env_key:
        credential = Credential(account=account or "env", api_key=env_key)
    elif env_token:
        credential = Credential(account=account or "env", token=env_token)
    elif account:
        credential = store.get_credential(account)

    return Resolved(url=url, credential=credential, account=account, profile=profile)
