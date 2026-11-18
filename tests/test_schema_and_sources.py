"""Tests for the schema version, the selector report, and the traffic log.

Each of these exists because of a specific way the profile could lie: a
profile written by an older version being read as if it were current, a
selector with no stability entry falling back to a guess, and an endpoint
that is referenced in a bundle but never actually called (or vice versa).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitesignup.mailbox import PROVIDERS, available_providers  # noqa: E402
from invitesignup.probe import TrafficLog  # noqa: E402
from invitesignup.profile import SCHEMA_VERSION, SiteProfile  # noqa: E402


def _profile(**overrides) -> SiteProfile:
    p = SiteProfile(
        site="example.test",
        base_url="https://example.test",
        invite_url="https://example.test/invite/CODE1",
        invite_code="CODE1",
        invite_valid=True,
        captcha="none",
        steps=["email", "password"],
        selectors={"email": 'input[type="email"]', "password": "#pw"},
    )
    p.verification.mode = "link"
    p.verification.pattern = r"https://example\.test/verify"
    for key, value in overrides.items():
        setattr(p, key, value)
    return p


def test_new_profiles_carry_the_schema_version():
    assert _profile().schema_version == SCHEMA_VERSION
    assert SiteProfile().schema_version == SCHEMA_VERSION


def test_old_profile_is_read_but_flagged():
    data = _profile().to_dict()
    data.pop("schema_version")
    old = SiteProfile.from_dict(data)
    assert old.schema_version == SCHEMA_VERSION      # upgraded in memory
    assert "schema" in old.confidence                # but it says so
    assert "older version" in old.confidence["schema"]


def test_current_profile_is_not_flagged():
    restored = SiteProfile.from_dict(_profile().to_dict())
    assert "schema" not in restored.confidence


def test_selector_report_derives_stability_from_one_place():
    p = _profile()
    p.selectors["submit"] = 'button:has-text("Continue")'
    report = {item["key"]: item for item in p.selector_report()}
    assert report["password"]["stability"] == "stable"
    assert report["submit"]["stability"] == "fragile"


def test_needs_review_names_the_fragile_selectors():
    p = _profile()
    p.selectors["submit"] = 'button:has-text("Continue")'
    reasons = p.needs_review()
    assert any("submit" in r and "fragile" in r for r in reasons), reasons


def test_needs_review_flags_missing_state_endpoints():
    p = _profile()
    assert any("account-state" in r for r in p.needs_review())
    p.endpoints_state = {"credits": "/api/credits"}
    assert not any("account-state" in r for r in p.needs_review())


def test_needs_review_flags_inferred_selectors():
    p = _profile()
    p.confidence = {"selectors": "inferred"}
    assert any("inferred" in r for r in p.needs_review())


def test_traffic_log_records_paths_and_methods():
    log = TrafficLog()
    log.add("GET", "https://example.test/api/auth/get-session")
    log.add("POST", "https://example.test/api/auth/get-session")
    log.add("GET", "https://example.test/static/app.js")   # not an API
    assert log.paths == ["/api/auth/get-session"]
    assert log.methods["/api/auth/get-session"] == ["GET", "POST"]


def test_traffic_log_ignores_non_api_and_duplicates():
    log = TrafficLog()
    log.add("GET", "https://example.test/api/credits")
    log.add("GET", "https://example.test/api/credits")
    log.add("GET", "https://example.test/favicon.ico")
    assert log.paths == ["/api/credits"]


def test_traffic_log_survives_a_malformed_url():
    log = TrafficLog()
    log.add("GET", "not a url")
    assert log.paths == []


def test_provider_registry_is_extensible():
    # The three built-ins must be registered, in the documented order.
    assert available_providers()[:3] == ["mail.tm", "tempmail.io", "guerrillamail"]
    assert set(available_providers()[:3]) <= set(PROVIDERS)


def test_registering_a_custom_provider_adds_it():
    from invitesignup.mailbox import Provider, register_provider

    @register_provider("test-only-provider")
    class _Custom(Provider):
        def create(self):
            return "x@y.test"

        def messages(self):
            return []

        def body(self, message_id):
            return ""

    try:
        assert "test-only-provider" in available_providers()
    finally:
        PROVIDERS.pop("test-only-provider", None)
