#  Project:      dfe-engine
#  File:         masking.py
#  Purpose:      Which names hold a credential, and the password in a URL
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Which names hold a credential, and the password in a URL.

scalo's :func:`~scalo.logger.filters.is_credential_name` decides whether a name holds
a credential. It matches a credential word anywhere in a name, which suits a log line
and hides settings an operator reads and writes over the API. :func:`credential_name`
shows those settings by the engine's own rules first, then asks scalo.
:mod:`dfe_engine.appmgmt.contract` reads documents back masked through it, and
:mod:`dfe_engine.source.models` refuses a literal credential with it.

scalo's :func:`~scalo.logger.filters.mask_url_userinfo` masks the password in
``scheme://user:password@host``, which runs to the last ``@`` the host follows, so a
password holding ``@`` or ``/`` goes whole. scalo's
:func:`~scalo.logger.filters.url_passwords` lists the passwords that masker replaces,
and :func:`hides_a_host` checks a written URL for one that hides its host.
"""

import re

from scalo.logger.filters import is_credential_name, url_passwords

_NAME_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
"""One word of a name: an acronym, a capitalised or lower-case run, or digits.

Every other character separates words, so ``bearer-tokens``, ``bearer_tokens``,
``bearerTokens`` and ``BEARER_TOKENS`` all read as ``bearer``, ``tokens``.
"""

SETTING_WORDS = frozenset(
    {
        "age",
        "at",
        "count",
        "dir",
        "endpoint",
        "env",
        "field",
        "file",
        "header",
        "interval",
        "length",
        "location",
        "login",
        "ms",
        "override",
        "path",
        "retries",
        "seconds",
        "secs",
        "size",
        "source",
        "timeout",
        "ttl",
        "type",
        "url",
    }
)
"""Last words that say a name holds where a credential is, how it is used, or how much.

``ssl_key_location``, ``token_url``, ``password_field``, ``credential_type``,
``session_timeout_ms`` and dfe-fetcher's ``session_login`` block each carry a
credential word and hold a setting, as do ``ping_interval``, ``password_min_length``,
``SESSION_TIMEOUT``, ``token_ttl`` and ``password_env``, which names the environment
variable holding the password. No name scalo lists ends in one of these.
"""

SHOWN_ENDINGS: frozenset[tuple[str, ...]] = frozenset(
    {("client", "auth"), ("private", "key", "id")}
)
"""Name endings that hold a setting: the TLS client-auth mode, and the public id of a key."""

OVERMATCHED_WORDS = frozenset({"mapping", "mappings", "oauth", "unauthenticated"})
"""Words that hold a credential word and are not one, so a name is judged without them.

``field_mapping`` holds ``pin`` and ``accept_unauthenticated`` holds ``auth``. An OAuth
flow's credentials carry their own words, so ``oauth_client_secret`` is still one and
``oauth_scope`` is not.
"""

FORM_WORDS = frozenset({"hash", "hashes", "digest", "digests", "json", "pem"})
"""A last word saying what form a value is held in, so the words before it are judged too.

``password_hash`` is a crackable digest of a password and ``tls.key_pem`` a private
key; ``config_hash`` and ``capture_json`` stay shown.
"""

CREDENTIAL_MAP = ("credentials",)
"""The words of a mapping name that holds credentials by name, so each value is one.

dfe-fetcher's ``auth.credentials`` maps a name to the credential its placements read.
Only a mapping named exactly so counts: ``oauth2_client_credentials`` holds settings
beside its secret. A list of that name holds placements instead, and each entry's
fields are judged by name.
"""

SECRET_GROUPS = ("secrets",)
"""The words of the chart mapping that switches each secret group on or off.

``secrets.hunt-runner.enabled`` is a toggle and is shown. Any other value under the
mapping is masked.
"""

_AUTHORITY_ENDS = frozenset("/?#\\")
"""Characters that end a URL's authority, so a host a client reads can sit after one."""


def words(name: str) -> tuple[str, ...]:
    """A name as lower-case words, split at separators and camelCase humps."""
    return tuple(word.lower() for word in _NAME_WORD.findall(name))


def _ends_in_any(parts: tuple[str, ...], endings: frozenset[tuple[str, ...]]) -> bool:
    """Whether ``parts`` ends in one of ``endings``."""
    return any(parts[-len(ending) :] == ending for ending in endings)


def credential_name(name: str, parent: str = "") -> bool:
    """Whether a field called ``name``, in the mapping named ``parent``, holds a credential.

    scalo's :func:`~scalo.logger.filters.is_credential_name` decides, after the engine's
    rules: a last word in :data:`SETTING_WORDS` or an ending in :data:`SHOWN_ENDINGS`
    holds a setting, and the words in :data:`OVERMATCHED_WORDS` are left out of the name
    and of ``parent``. The name is judged again without a trailing word in
    :data:`FORM_WORDS`, so ``key_pem`` under ``tls`` is the TLS key. ``parent`` qualifies
    only a bare ``key`` or ``value``, as scalo reads it.
    """
    parts = words(name)
    if not parts or parts[-1] in SETTING_WORDS or _ends_in_any(parts, SHOWN_ENDINGS):
        return False
    kept = tuple(word for word in parts if word not in OVERMATCHED_WORDS)
    if not kept:
        return False
    holder = "_".join(word for word in words(parent) if word not in OVERMATCHED_WORDS)
    spellings = [name if kept == parts else "_".join(kept)]
    stem = kept
    while len(stem) > 1 and stem[-1] in FORM_WORDS:
        stem = stem[:-1]
    if stem != kept:
        spellings.append("_".join(stem))
    return any(is_credential_name(spelling, holder) for spelling in spellings)


def hides_a_host(text: str) -> bool:
    r"""Whether a URL password in ``text`` holds a character that ends a URL's authority.

    A client reads the host after the last ``@`` before the first ``/``, ``?``, ``#`` or
    ``\``, so a password holding one of those can hide the host a client connects to.
    """
    return any(_AUTHORITY_ENDS.intersection(password) for password in url_passwords(text))
