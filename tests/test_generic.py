"""The pipeline must not assume any particular site's conventions.

An earlier version defaulted ``sign_in_endpoint`` and the session endpoint to
Better Auth paths (``/api/auth/sign-in/email``, ``/api/auth/get-session``).
That made the tool quietly Yila-shaped: it appeared to work on any site whose
profile came from a Better Auth site and failed everywhere else with a
misleading message. These tests pin the generic behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitesignup.profile import SiteProfile  # noqa: E402
from invitesignup.verify import sign_in, verify_account  # noqa: E402


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


def test_new_profile_has_no_sign_in_endpoint():
    # No default: the prober fills it in, or the profile is honest about
    # lacking it.
    assert SiteProfile().sign_in_endpoint == ""


def test_needs_review_flags_a_missing_sign_in_endpoint():
    reasons = _profile().needs_review()
    assert any("sign-in endpoint" in r for r in reasons), reasons


def test_needs_review_is_quiet_once_it_is_set():
    p = _profile(sign_in_endpoint="/login")
    assert not any("sign-in endpoint" in r for r in p.needs_review())


def test_sign_in_refuses_to_guess_an_endpoint():
    # There is no universal sign-in path.  Guessing one produces a confusing
    # 404 rather than "this profile cannot verify".
    try:
        sign_in("https://example.test", "a@b.test", "pw", endpoint="")
    except ValueError as exc:
        assert "no sign-in endpoint" in str(exc)
    else:
        raise AssertionError("sign_in should refuse an empty endpoint")


def test_verify_reports_the_missing_endpoint_instead_of_failing_obscurely():
    report = verify_account(_profile(), "a@b.test", "pw")
    assert report.verified is False
    assert "no sign-in endpoint" in report.reason
    # And it says so as a named check, not just a reason string.
    assert any(c["name"] == "sign-in" and not c["ok"] for c in report.checks)


def test_verify_accepts_a_site_specific_endpoint():
    # The signature must not require a Better Auth path.
    p = _profile(sign_in_endpoint="/custom/login")
    report = verify_account(p, "a@b.test", "pw", sign_in_endpoint=p.sign_in_endpoint)
    # It will fail to connect; what matters is that it tried the given path
    # rather than refusing on configuration grounds.
    assert "no sign-in endpoint" not in report.reason


def test_email_only_form_is_automatable():
    # Magic-link and OTP-only sites have no password field.  The verdict must
    # allow them, and the runner must not then reject them for the very thing
    # the verdict approved.
    p = _profile(steps=["email"], selectors={"email": 'input[type="email"]'})
    from invitesignup.verdict import AUTOMATABLE, decide

    v = decide(p)
    assert v.state == AUTOMATABLE, v.reasons
    assert v.allowed is True


def test_verdict_requires_a_password_selector_only_when_a_password_step_exists():
    from invitesignup.verdict import BLOCKED_FACTS, decide

    p = _profile(steps=["email", "password"], selectors={"email": 'input[type="email"]'})
    v = decide(p)
    assert v.state == BLOCKED_FACTS
    assert any("missing_selectors" in b for b in v.blockers)


def test_absolute_endpoint_in_a_profile_is_not_double_prefixed():
    # Writing a full URL where a path was expected is an easy hand-edit
    # mistake; concatenating gave "https://sitehttps://site/api/me".
    p = _profile(endpoints={"session": "https://other.test/api/me"})
    assert p.absolute("https://other.test/api/me") == "https://other.test/api/me"


def test_relative_endpoint_resolves_against_the_base_url():
    p = _profile()
    assert p.absolute("/api/me") == "https://example.test/api/me"
    assert p.absolute("api/me") == "https://example.test/api/me"
    assert p.absolute("") == ""


def test_base_url_with_trailing_slash_does_not_double_up():
    p = _profile(base_url="https://example.test/")
    assert p.absolute("/api/me") == "https://example.test/api/me"


def test_verify_helper_joins_the_same_way():
    from invitesignup.verify import _absolute

    assert _absolute("https://example.test", "/api/me") == "https://example.test/api/me"
    assert _absolute("https://example.test/", "api/me") == "https://example.test/api/me"
    assert _absolute("https://example.test", "https://other.test/x") == "https://other.test/x"
    assert _absolute("https://example.test", "") == "https://example.test"


def test_a_page_without_a_form_is_reported_as_such_not_as_a_bad_invite():
    """make.fun's invite page has an "invited you" banner and a Sign in button.

    Its actual signup is a Solana wallet signature.  Reporting that as "no
    invite code found" sends the reader hunting for a broken link that is
    perfectly fine -- the invite code is valid, the page just has no form.
    """
    from invitesignup.verdict import BLOCKED_WALLET, decide

    p = _profile(form_less_signup=True, invite_code="", invite_valid=None)
    v = decide(p)
    assert v.state == BLOCKED_WALLET
    assert "no_signup_form" in v.blockers
    # The invite-code wording must not be what the user sees.
    assert not any("invite code did not validate" in r for r in v.reasons)
    assert any("no email or password field" in r for r in v.reasons)


def test_a_wallet_endpoint_alone_is_enough_to_stop():
    from invitesignup.verdict import BLOCKED_WALLET, decide

    p = _profile(endpoints={"sign-up": "/api/auth/solana/key"})
    v = decide(p)
    assert v.state == BLOCKED_WALLET
    assert "wallet_signup" in v.blockers


def test_a_normal_form_site_is_not_mistaken_for_a_wallet_site():
    from invitesignup.verdict import AUTOMATABLE, decide

    p = _profile(form_less_signup=False,
                 endpoints={"sign-up": "/api/auth/sign-up/email"})
    assert decide(p).state == AUTOMATABLE
