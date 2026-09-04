"""The unit suite must not post to the release endpoint on app creation."""

from scalo.version_check.checker import VersionCheckConfig


def test_version_check_is_disabled_under_test():
    # tests/conftest.py pins version_check.enabled off before scalo's cascade
    # loads; create_app() resolves its config through the same call.
    cfg = VersionCheckConfig.from_cascade_or(api_url="https://releases.example.test/check")
    assert cfg.enabled is False
