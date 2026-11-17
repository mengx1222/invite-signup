"""Offline tests for the profile model and the verdict logic.

The verdict is the safety decision of the whole pipeline, so it gets the
most explicit coverage: a captcha must stop the run, unknown verification
must stop the run, and terms that forbid automation must stop the run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitesignup.ledger import dedupe_key, scrub  # noqa: E402
from invitesignup.profile import SiteProfile  # noqa: E402
from invitesignup.verdict import (  # noqa: E402
    AUTOMATABLE,
    BLOCKED_CAPTCHA,
    BLOCKED_FACTS,
    BLOCKED_TOS,
    decide,
)


def base_profile() -> SiteProfile:
    p = SiteProfile(
        site="example.com",
        base_url="https://example.com",
        invite_url="https://example.com/invite/ABC123",
        invite_code="ABC123",
        invite_valid=True,
        captcha="none",
        steps=["email", "password"],
        selectors={"email": 'input[type="email"]', "password": "#pw"},
    )
    p.verification.mode = "link"
    p.verification.pattern = r"https://example\.com/verify\?token="
    return p


def test_verdict_allows_a_clean_profile():
    v = decide(base_profile())
    assert v.state == AUTOMATABLE
    assert v.allowed is True


def test_verdict_blocks_on_captcha():
    p = base_profile()
    p.captcha = "turnstile"
    v = decide(p)
    assert v.state == BLOCKED_CAPTCHA
    assert v.allowed is False


def test_verdict_blocks_when_terms_forbid_automation():
    v = decide(base_profile(), tos_forbids_automation=True)
    assert v.state == BLOCKED_TOS
    assert v.allowed is False


def test_verdict_blocks_when_invite_is_invalid():
    p = base_profile()
    p.invite_valid = False
    v = decide(p)
    assert v.state == BLOCKED_FACTS
    assert v.allowed is False


def test_verdict_blocks_on_unknown_verification():
    p = base_profile()
    p.verification.mode = "unknown"
    v = decide(p)
    assert v.state == BLOCKED_FACTS
    assert "verification_mode_unknown" in v.blockers


def test_verdict_blocks_when_email_selector_missing():
    p = base_profile()
    p.selectors.pop("email")
    v = decide(p)
    assert v.state == BLOCKED_FACTS
    assert any(b.startswith("missing_selectors") for b in v.blockers)


def test_verdict_requires_a_pattern_for_link_verification():
    p = base_profile()
    p.verification.mode = "link"
    p.verification.pattern = ""
    v = decide(p)
    assert v.state == BLOCKED_FACTS
    assert "missing_link_pattern" in v.blockers


def test_verdict_allows_link_verification_with_a_pattern():
    p = base_profile()
    p.verification.mode = "link"
    p.verification.pattern = r"https://example\.com/verify\?token="
    v = decide(p)
    assert v.state == AUTOMATABLE


def test_verdict_flags_fragile_selectors():
    p = base_profile()
    p.selectors["submit"] = 'button:has-text("Continue")'
    v = decide(p)
    assert any("fragile" in r for r in v.reasons)


def test_profile_roundtrip(tmp_path=None):
    p = base_profile()
    p.verification.pattern = "x"
    restored = SiteProfile.from_dict(p.to_dict())
    assert restored.site == p.site
    assert restored.verification.pattern == "x"
    assert restored.selectors == p.selectors


def test_selector_stability_classification():
    p = base_profile()
    p.selectors["a"] = "#invite-password"
    p.selectors["b"] = 'input[name="email"]'
    p.selectors["c"] = 'button:has-text("Continue")'
    assert p.stability_of("a") == "stable"
    assert p.stability_of("b") == "stable"
    assert p.stability_of("c") == "fragile"
    assert "c" in p.fragile_selectors()


def test_ledger_dedupe_key_is_stable_per_day():
    assert dedupe_key("yila.ai", "YL-1", "2026-11-17") == "invite-signup:yila.ai:YL-1:2026-11-17"


def test_ledger_scrubs_secrets():
    row = scrub({"email": "a@b.c", "password": "hunter2", "api_key": "sk-1", "site": "x"})
    assert row["email"] == "a@b.c"
    assert row["password"] == "<redacted>"
    assert row["api_key"] == "<redacted>"
    assert row["site"] == "x"


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
