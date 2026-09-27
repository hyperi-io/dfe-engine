#  Project:      dfe-engine
#  File:         synthetic_data/entities.py
#  Purpose:      Seeded entity pool - coherent hosts/users/accounts across events
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Seeded entity pool.

Vanilla fakers emit a fresh random value per call, which is exactly what makes
generated data look fake: the same "host" never keeps its IP. The pool fixes
that by minting a small fleet of hosts, users and cloud accounts up front -
each internally coherent (hostname <-> FQDN <-> IP <-> MAC, user <-> email on
the org domain) - and drawing from them with a heavy-tailed weighting so some
entities are visibly busier than others, as in real telemetry.

Identical seed => identical pool => identical stream.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from faker import Faker

# (role prefix, fleet weight, linux-likelihood) - weight shapes how many of the
# fleet carry the role; linux-likelihood keeps dc/mail boxes windows-heavy.
_HOST_ROLES: tuple[tuple[str, int, float], ...] = (
    ("web", 6, 0.95),
    ("app", 5, 0.9),
    ("db", 3, 0.9),
    ("k8s-node", 4, 1.0),
    ("proxy", 2, 1.0),
    ("build", 2, 0.9),
    ("dc", 1, 0.05),
    ("mail", 1, 0.3),
)

_AWS_REGIONS = (
    "us-east-1",
    "us-west-2",
    "eu-west-1",
    "eu-central-1",
    "ap-southeast-2",
    "ap-northeast-1",
)


@dataclass(frozen=True, slots=True)
class Host:
    """A generated host with internally coherent identity."""

    hostname: str
    fqdn: str
    ipv4: str
    mac: str
    os: str


@dataclass(frozen=True, slots=True)
class User:
    """A generated user on the org domain."""

    username: str
    email: str
    uid: int


@dataclass(frozen=True, slots=True)
class CloudAccount:
    """A generated cloud account with its home regions."""

    account_id: str
    regions: tuple[str, ...]


class EntityPool:
    """A deterministic pool of coherent entities.

    Args:
        seed: Seed for both the RNG and the Faker instance. ``None`` gives a
            non-reproducible pool.
        hosts: Fleet size.
        users: User population size.
        accounts: Cloud account count.
        org_domain: Org domain for FQDNs/emails; faked when omitted.
    """

    def __init__(
        self,
        seed: int | None = None,
        *,
        hosts: int = 24,
        users: int = 32,
        accounts: int = 3,
        org_domain: str | None = None,
    ) -> None:
        # Seeded reproducibility is the feature; nothing here is security-sensitive.
        self.rng = random.Random(seed)  # noqa: S311
        self.fake = Faker()
        self.fake.seed_instance(seed)
        self.org_domain = org_domain or self.fake.domain_name()

        self._hosts = self._build_hosts(hosts)
        self._users = self._build_users(users)
        self._accounts = self._build_accounts(accounts)
        # Heavy-tailed draw weights (1/rank): a few entities dominate the
        # stream, the long tail appears occasionally - like real telemetry.
        self._host_weights = [1.0 / (i + 1) for i in range(len(self._hosts))]
        self._user_weights = [1.0 / (i + 1) for i in range(len(self._users))]

    # -- construction ----------------------------------------------

    def _build_hosts(self, count: int) -> list[Host]:
        roles = [role for role, weight, _ in _HOST_ROLES for _ in range(weight)]
        linux_odds = {role: odds for role, _, odds in _HOST_ROLES}
        # One /24 per role keeps addressing plausible (web boxes cluster).
        subnets = {role: 20 + i for i, (role, _, _) in enumerate(_HOST_ROLES)}
        counters: dict[str, int] = {}
        hosts: list[Host] = []
        for _ in range(count):
            role = self.rng.choice(roles)
            counters[role] = counters.get(role, 0) + 1
            n = counters[role]
            hostname = f"{role}-{n:02d}"
            hosts.append(
                Host(
                    hostname=hostname,
                    fqdn=f"{hostname}.{self.org_domain}",
                    ipv4=f"10.{subnets[role]}.0.{n + 10}",
                    mac=self.fake.mac_address(),
                    os="linux" if self.rng.random() < linux_odds[role] else "windows",
                )
            )
        return hosts

    def _build_users(self, count: int) -> list[User]:
        users: list[User] = []
        seen: set[str] = set()
        while len(users) < count:
            first = self.fake.first_name().lower()
            last = self.fake.last_name().lower()
            username = f"{first[0]}{last}"
            if username in seen:
                username = f"{first[0]}{last}{self.rng.randint(1, 99)}"
            if username in seen:
                continue
            seen.add(username)
            users.append(
                User(
                    username=username,
                    email=f"{username}@{self.org_domain}",
                    uid=1000 + len(users),
                )
            )
        return users

    def _build_accounts(self, count: int) -> list[CloudAccount]:
        accounts: list[CloudAccount] = []
        for _ in range(count):
            regions = tuple(self.rng.sample(_AWS_REGIONS, k=self.rng.randint(1, 3)))
            accounts.append(
                CloudAccount(
                    account_id=f"{self.rng.randrange(10**11, 10**12)}",
                    regions=regions,
                )
            )
        return accounts

    # -- draws -----------------------------------------------------

    def host(self) -> Host:
        """Draw a host, heavy-tailed."""
        return self.rng.choices(self._hosts, weights=self._host_weights, k=1)[0]

    def user(self) -> User:
        """Draw a user, heavy-tailed."""
        return self.rng.choices(self._users, weights=self._user_weights, k=1)[0]

    def account(self) -> CloudAccount:
        """Draw a cloud account, uniform."""
        return self.rng.choice(self._accounts)

    def external_ipv4(self) -> str:
        """A public (non-pool) IPv4 - callers, scanners, cloud endpoints."""
        return self.fake.ipv4_public()

    @property
    def hosts(self) -> list[Host]:
        """The full fleet (read-only use: tests, docs)."""
        return list(self._hosts)

    @property
    def users(self) -> list[User]:
        """The full user population (read-only use: tests, docs)."""
        return list(self._users)

    @property
    def accounts(self) -> list[CloudAccount]:
        """All cloud accounts (read-only use: tests, docs)."""
        return list(self._accounts)
