#  Project:      dfe-engine
#  File:         tests/unit/test_masking.py
#  Purpose:      Tests for the credential-name judgement and the URL password grammar
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine's rules over scalo's credential matcher, and where a URL's password ends."""

import random
import time

import pytest
from scalo.logger.filters import MASK_VALUE, SENSITIVE_FIELDS

from dfe_engine import masking

_AUTHORITY_ENDS = "/?#"


def _password_end(text: str, start: int, run_end: int) -> int | None:
    """Where the password starting at ``start`` ends: the last ``@`` a host follows, else None."""
    for at in range(run_end - 1, start - 1, -1):
        if text[at] != "@":
            continue
        host_end = at + 1
        while host_end < len(text) and not (
            text[host_end].isspace() or text[host_end] in "@" + _AUTHORITY_ENDS
        ):
            host_end += 1
        if host_end == len(text) or text[host_end].isspace() or text[host_end] in _AUTHORITY_ENDS:
            return at
    return None


def _reference(text: str, *, whole: bool, mask: str) -> str:
    """The grammar read step by step: scheme, user, colon, then a password up to the last ``@`` a host follows.

    A password runs no further than whitespace or the next ``://``. ``whole`` masks the user too.
    """
    out: list[str] = []
    pos = 0
    head = text.find("://")
    while head != -1:
        user_end = head + 3
        while user_end < len(text) and not (text[user_end].isspace() or text[user_end] in ":/@"):
            user_end += 1
        if user_end < len(text) and text[user_end] == ":":
            start = user_end + 1
            run_end = start
            while (
                run_end < len(text)
                and not text[run_end].isspace()
                and not text.startswith("://", run_end)
            ):
                run_end += 1
            end = _password_end(text, start, run_end)
            if end is not None:
                if whole:
                    out.append(text[pos:head] + "://" + mask)
                elif end > start:
                    out.append(text[pos:start] + mask)
                else:
                    out.append(text[pos:end])
                pos = end
                head = text.find("://", end)
                continue
        head = text.find("://", head + 1)
    out.append(text[pos:])
    return "".join(out)


class TestUrlPassword:
    @pytest.mark.parametrize(
        ("text", "shown"),
        [
            ("postgres://dfe:pw-4501@db:5432/dfe", f"postgres://dfe:{MASK_VALUE}@db:5432/dfe"),
            ("postgres://dfe:pa/ss@db:5432/dfe", f"postgres://dfe:{MASK_VALUE}@db:5432/dfe"),
            ("postgres://dfe:pa@ss/x@db/dfe", f"postgres://dfe:{MASK_VALUE}@db/dfe"),
            ("kafka://svc:pw@b1:9092,b2:9092", f"kafka://svc:{MASK_VALUE}@b1:9092,b2:9092"),
            ("a=x://u:p@h1 b=y://v:q@h2", f"a=x://u:{MASK_VALUE}@h1 b=y://v:{MASK_VALUE}@h2"),
            (
                '{"a":"x://u:p@h1","b":"y://v:q@h2"}',
                f'{{"a":"x://u:{MASK_VALUE}@h1","b":"y://v:{MASK_VALUE}@h2"}}',
            ),
            ("http://user@host/x", "http://user@host/x"),
            ("https://host:8443/path", "https://host:8443/path"),
            ("https://host:8443/@scope/pkg", f"https://host:{MASK_VALUE}@scope/pkg"),
            ("http://user:@host", "http://user:@host"),
            ("no url here", "no url here"),
            ("", ""),
        ],
    )
    def test_the_password_is_masked_up_to_the_last_at_sign_a_host_follows(self, text, shown):
        assert masking.mask_url_password(text) == shown

    def test_the_whole_userinfo_goes_for_a_log_line(self):
        line = "Push to http://dfe-admin:s3c@r/t@forge.svc:3000/deploy.git successful."
        assert (
            masking.mask_url_userinfo(line)
            == "Push to http://***@forge.svc:3000/deploy.git successful."
        )

    def test_the_passwords_are_listed_whole(self):
        assert masking.url_passwords("x://u:a/b@h y://v:@h z://w:c@d@h") == ["a/b", "c@d"]

    @pytest.mark.parametrize(
        ("text", "hides"),
        [
            ("https://u:new@attacker.example?x@broker/x", True),
            ("https://u:new@attacker.example#x@broker/x", True),
            ("https://u:new@attacker.example/x@broker/x", True),
            ("https://u:new@attacker.example\\x@broker/x", True),
            ("https://u:n@w@broker/x", False),
            ("https://broker/x", False),
        ],
    )
    def test_a_password_that_can_hide_a_host_is_told_apart(self, text, hides):
        assert masking.hides_a_host(text) is hides

    def test_masks_as_the_reference_reads_it_on_random_text(self):
        rng = random.Random(4504)
        alphabet = ["a", "B", "1", ".", ",", ":", ":", "/", "/", "@", "@", "?", "#", " ", "\\"]
        for _ in range(20_000):
            text = "".join(rng.choices(alphabet, k=rng.randint(0, 24)))
            assert masking.mask_url_password(text) == _reference(
                text, whole=False, mask=MASK_VALUE
            ), repr(text)
            assert masking.mask_url_userinfo(text) == _reference(text, whole=True, mask="***"), (
                repr(text)
            )

    @pytest.mark.parametrize(
        "text",
        [
            "A" * 200_000 + " a://x",
            "a://:" + "A" * 200_000,
            "1a" * 100_000 + " b://x",
            "a://:" + "@" * 200_000,
            "a://:" + "@a" * 100_000,
            "a://:" + "@" + "a" * 200_000 + "@",
            "://a:" * 50_000,
            "://" + "a" * 200_000,
        ],
        ids=[
            "letter-run",
            "after-userinfo",
            "mixed-run",
            "at-signs",
            "short-hosts",
            "one-long-host",
            "repeated-heads",
            "user-with-no-colon",
        ],
    )
    def test_a_long_value_is_read_in_linear_time(self, text):
        started = time.perf_counter()
        masking.mask_url_password(text)
        masking.mask_url_userinfo(text)
        assert time.perf_counter() - started < 2.0


class TestCredentialName:
    @pytest.mark.parametrize(
        ("name", "parent"),
        [
            ("secretAccessKey", ""),
            ("x-api-key", ""),
            ("api.key", ""),
            ("sasl.jaas.config", ""),
            ("PGPASSWORD", ""),
            ("api_key_v2", ""),
            ("key_pem", "tls"),
            ("key", "tls"),
            ("value", "header"),
            ("oauth_client_secret", ""),
            ("password_hash", ""),
            ("session_id", ""),
            ("AWS_ACCESS_KEY_ID", ""),
        ],
    )
    def test_a_credential_in_any_spelling_is_one(self, name, parent):
        assert masking.credential_name(name, parent) is True

    @pytest.mark.parametrize(
        ("name", "parent"),
        [
            ("token_url", ""),
            ("ssl_key_location", ""),
            ("password_field", ""),
            ("session_timeout_ms", ""),
            ("session_login", "auth"),
            ("client_auth", "tls"),
            ("private_key_id", "auth"),
            ("field_mapping", ""),
            ("mappings", "coercion"),
            ("accept_unauthenticated", ""),
            ("oauth_scope", ""),
            ("oauth_client_id", ""),
            ("key", "field_mapping"),
            ("key", "kafka"),
            ("config_hash", ""),
        ],
    )
    def test_a_setting_that_holds_a_credential_word_is_not(self, name, parent):
        assert masking.credential_name(name, parent) is False

    def test_no_name_scalo_lists_ends_in_a_setting_word(self):
        # A setting word ending a listed name would show a credential scalo masks.
        endings = {masking.words(term)[-1] for term in SENSITIVE_FIELDS}
        assert endings.isdisjoint(masking.SETTING_WORDS)

    def test_no_name_scalo_lists_ends_in_a_shown_ending(self):
        listed = {masking.words(term) for term in SENSITIVE_FIELDS}
        assert not any(
            term[-len(ending) :] == ending for term in listed for ending in masking.SHOWN_ENDINGS
        )

    def test_no_name_scalo_lists_holds_an_overmatched_word(self):
        listed = {word for term in SENSITIVE_FIELDS for word in masking.words(term)}
        assert listed.isdisjoint(masking.OVERMATCHED_WORDS)
