"""A rule name names one file inside the rules directory, or nothing at all."""

from pathlib import Path

import pytest

from dfe_engine.hunts.rule_names import RuleNameError, rule_file, validate_rule_name

_UUID_HEX = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


@pytest.mark.parametrize(
    "name",
    [
        "certutil",
        "win_cert_01",
        "post-1234-abcd-rule",
        "win.cert",
        f"sigma_windows-audit_{_UUID_HEX}",
    ],
)
def test_a_real_rule_name_names_its_file_in_the_directory(tmp_path: Path, name: str):
    assert rule_file(tmp_path, name, ".yaml") == tmp_path / f"{name}.yaml"


@pytest.mark.parametrize(
    "name",
    ["../x", "a/b", "/etc/passwd", "", "..", "a..b", "a\x00b", "a\\b", "a\nb", " a"],
)
def test_a_name_that_could_leave_the_directory_is_refused(tmp_path: Path, name: str):
    with pytest.raises(RuleNameError):
        rule_file(tmp_path, name, ".yaml")


def test_the_refusal_is_a_value_error_so_existing_handlers_see_it():
    with pytest.raises(ValueError, match="rule name"):
        validate_rule_name("../x")


def test_a_name_of_128_characters_is_accepted_and_129_refused():
    assert validate_rule_name("r" * 128) == "r" * 128

    with pytest.raises(RuleNameError, match="at most 128"):
        validate_rule_name("r" * 129)


def test_a_symlink_pointing_outside_the_directory_is_refused(tmp_path: Path):
    rules = tmp_path / "rules"
    rules.mkdir()
    secret = tmp_path / "secret.yaml"
    secret.write_text("where_clause: '1 = 1'\n", encoding="utf-8")
    (rules / "leak.yaml").symlink_to(secret)

    with pytest.raises(RuleNameError, match="outside the rules directory"):
        rule_file(rules, "leak", ".yaml")


def test_a_symlink_that_stays_inside_the_directory_is_kept(tmp_path: Path):
    (tmp_path / "real.yaml").write_text("where_clause: 'a = 1'\n", encoding="utf-8")
    (tmp_path / "alias.yaml").symlink_to(tmp_path / "real.yaml")

    assert rule_file(tmp_path, "alias", ".yaml") == tmp_path / "alias.yaml"


def test_a_rules_directory_reached_through_a_symlink_still_works(tmp_path: Path):
    # git-sync publishes its checkout behind a symlink, so the directory itself is one.
    checkout = tmp_path / "checkout" / "rules"
    checkout.mkdir(parents=True)
    (checkout / "certutil.yaml").write_text("where_clause: 'a = 1'\n", encoding="utf-8")
    current = tmp_path / "current"
    current.symlink_to(checkout)

    assert rule_file(current, "certutil", ".yaml") == current / "certutil.yaml"
