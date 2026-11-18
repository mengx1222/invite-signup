"""Offline tests for the prober's pure functions.

No network, no browser: these feed synthetic documents through the same
code paths that a live site goes through.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitesignup.probe import (  # noqa: E402
    CAPTCHA_KEYWORDS,
    DomInfo,
    classify_captcha,
    extract_chunks,
    extract_invite_payload,
    find_sign_in_endpoint,
    find_state_endpoints,
    infer_attribution,
    infer_selectors,
    infer_steps,
    infer_verification,
    looks_incidental,
    role_endpoints,
)

RSC_HTML = (
    '<html><head><script src="/_next/static/chunks/aaa.js"></script>'
    '<script src="/_next/static/chunks/bbb.js"></script></head><body>'
    '<script>self.__next_f.push([1,"3:{\\"landingData\\":{\\"valid\\":true,'
    '\\"referralCode\\":\\"YL-TEST0000\\",\\"inviterName\\":\\"test inviter\\",'
    '\\"rewardCredits\\":200,\\"hasRewardSlots\\":true}}"])</script>'
    '</body></html>'
)


def test_extract_invite_payload_from_rsc():
    payload = extract_invite_payload(RSC_HTML)
    assert payload["valid"] is True
    assert payload["referralCode"] == "YL-TEST0000"
    assert payload["rewardCredits"] == 200


def test_extract_invite_payload_returns_empty_when_absent():
    assert extract_invite_payload("<html><body>nothing</body></html>") == {}


def test_extract_chunks_uses_base_url():
    chunks = extract_chunks(RSC_HTML, "https://example.com")
    assert "https://example.com/_next/static/chunks/aaa.js" in chunks
    assert "https://example.com/_next/static/chunks/bbb.js" in chunks


def test_classify_captcha_detects_turnstile_in_the_live_dom():
    dom = DomInfo(frames=["https://challenges.cloudflare.com/turnstile/v0/api.js"])
    assert classify_captcha({"captcha_hits": {}}, "", dom) == "turnstile"


def test_classify_captcha_treats_a_bundle_keyword_as_suspected_only():
    # A library mentioning "hcaptcha" is not a challenge.  This exact false
    # positive stopped a real, captcha-free site from being automated.
    scan = {"captcha_hits": {"hcaptcha": ["https://example.com/chunk.js"]}}
    assert classify_captcha(scan, "", DomInfo()) == "suspected"


def test_classify_captcha_detects_a_captcha_widget_element():
    dom = DomInfo(captcha_nodes=1)
    assert classify_captcha({"captcha_hits": {}}, "", dom) == "captcha"


def test_classify_captcha_none_when_clean():
    assert classify_captcha({"captcha_hits": {}}, "<html></html>", DomInfo()) == "none"


def test_adsense_recaptcha_frame_is_not_a_challenge():
    # Google's ad stack loads google.com/recaptcha/api2/aframe on any page
    # with AdSense.  It is an ad-quality frame, not a challenge shown to the
    # user, and counting it made a plain signup page report "recaptcha".
    dom = DomInfo(frames=["https://www.google.com/recaptcha/api2/aframe"])
    assert classify_captcha({"captcha_hits": {}}, "", dom) == "none"


def test_ad_network_frames_are_ignored():
    dom = DomInfo(frames=[
        "https://googleads.g.doubleclick.net/pagead/ads?client=ca-pub-1",
        "https://ep2.adtrafficquality.google/sodar/sodar2/257/runner.html",
        "https://tpc.googlesyndication.com/safeframe/1-0-1/html/container.html",
    ])
    assert classify_captcha({"captcha_hits": {}}, "", dom) == "none"


def test_a_real_recaptcha_frame_still_counts():
    dom = DomInfo(frames=["https://www.google.com/recaptcha/api2/anchor?k=abc"])
    assert classify_captcha({"captcha_hits": {}}, "", dom) == "recaptcha"


def test_captcha_keywords_are_tuples_of_needles():
    # A single-element tuple written as ("hcaptcha") is a *string*, and
    # iterating it yields characters.  Every chunk then matched on "h" and
    # a captcha-free site was declared hcaptcha-protected.
    for kind, needles in CAPTCHA_KEYWORDS.items():
        assert isinstance(needles, tuple), f"{kind} must be a tuple, got {type(needles).__name__}"
        for needle in needles:
            assert isinstance(needle, str)
            assert len(needle) > 1, f"{kind} needle {needle!r} is too short to be a keyword"


def test_role_endpoints_maps_paths():
    paths = [
        "/api/referral/check-email",
        "/api/auth/sign-up/email",
        "/api/auth/get-session",
        "/api/credits",
    ]
    roles = role_endpoints(paths)
    assert roles["check-email"] == "/api/referral/check-email"
    assert roles["sign-up"] == "/api/auth/sign-up/email"
    assert roles["session"] == "/api/auth/get-session"
    assert roles["credits"] == "/api/credits"


def test_role_endpoints_composes_base_path_with_fragment():
    # Auth clients build their routes at runtime: the bundle holds
    # "/verify-email" and a base path "/api/auth".  Scanning only for full
    # "/api/..." strings misses the verification route entirely.
    roles = role_endpoints(
        ["/api/auth", "/api/referral/check-email"],
        ["/verify-email", "/get-session", "/sign-up/email"],
    )
    assert roles["verify"] == "/api/auth/verify-email"
    assert roles["session"] == "/api/auth/get-session"
    assert roles["sign-up"] == "/api/auth/sign-up/email"


def test_infer_selectors_prefers_id_over_type():
    dom = DomInfo(
        inputs=[
            {"type": "email", "id": "", "name": "", "testid": "", "placeholder": ""},
            {"type": "password", "id": "invite-password", "name": "", "testid": "", "placeholder": ""},
            {"type": "password", "id": "invite-confirm-password", "name": "", "testid": "", "placeholder": ""},
        ],
        buttons=[{"type": "submit", "text": "Continue", "id": "", "disabled": False}],
    )
    selectors = infer_selectors(dom)
    assert selectors["email"] == 'input[type="email"]'
    assert selectors["password"] == "#invite-password"
    assert selectors["confirm_password"] == "#invite-confirm-password"
    assert selectors["submit"] == 'button[type="submit"]'


def test_infer_steps():
    dom = DomInfo(
        inputs=[{"type": "email", "id": "", "name": "", "testid": "", "placeholder": ""},
                {"type": "password", "id": "pw", "name": "", "testid": "", "placeholder": ""}],
        buttons=[{"type": "submit", "text": "Continue", "id": "", "disabled": False}],
    )
    assert infer_steps(dom) == ["email", "password"]


def test_infer_verification_detects_code_mode():
    dom = DomInfo(inputs=[{"type": "text", "id": "verification-code", "name": "code",
                            "testid": "", "placeholder": ""}])
    v = infer_verification({"api_paths": [], "verify_path_samples": []}, dom)
    assert v["mode"] == "code"
    assert v["pattern"]


def test_infer_verification_detects_link_mode():
    dom = DomInfo(inputs=[{"type": "email", "id": "email", "name": "", "testid": "", "placeholder": ""}])
    v = infer_verification({"api_paths": ["/api/auth/verify-email"], "verify_path_samples": []}, dom)
    assert v["mode"] == "link"


def test_infer_verification_unknown_when_nothing_found():
    dom = DomInfo(inputs=[{"type": "email", "id": "email", "name": "", "testid": "", "placeholder": ""}])
    v = infer_verification({"api_paths": [], "verify_path_samples": []}, dom)
    assert v["mode"] == "unknown"


def test_infer_attribution_finds_cookie_name():
    html = '{"yila_invite_code":"YL-1"}'
    attribution = infer_attribution(html, "https://yila.ai/en/invite/YL-1")
    assert attribution["kind"] == "cookie"
    assert attribution["name"] == "yila_invite_code"


def test_infer_attribution_falls_back_to_query():
    attribution = infer_attribution("", "https://example.com/signup?ref=ABC123")
    assert attribution["kind"] == "query"
    assert attribution["name"] == "ref"


def test_infer_attribution_prefers_the_set_cookie_header():
    attribution = infer_attribution(
        "", "https://yila.ai/en/invite/YL-1", ["NEXT_LOCALE", "yila_invite_code"]
    )
    assert attribution["kind"] == "cookie"
    assert attribution["name"] == "yila_invite_code"


def test_generated_ids_are_not_preferred():
    dom = DomInfo(
        inputs=[{"type": "email", "id": "base-ui-_R_79av5ubql3ivb_", "name": "",
                 "testid": "", "placeholder": ""}],
        buttons=[],
    )
    selectors = infer_selectors(dom)
    assert selectors["email"] == 'input[type="email"]'


def test_find_state_endpoints_ignores_action_paths():
    paths = [
        "/api/referral/check-email",   # an action, not a state read
        "/api/credits",
        "/api/referral/me",
    ]
    found = find_state_endpoints(paths)
    assert found["credits"] == "/api/credits"
    assert found["referral"] == "/api/referral/me"


def test_find_state_endpoints_returns_empty_when_absent():
    assert find_state_endpoints(["/api/auth/get-session"]) == {}


def test_find_sign_in_endpoint_from_a_full_path():
    assert find_sign_in_endpoint(["/api/auth/sign-in/email"], []) == "/api/auth/sign-in/email"


def test_find_sign_in_endpoint_composed_from_a_fragment():
    assert find_sign_in_endpoint(["/api/auth"], ["/sign-in/email"]) == "/api/auth/sign-in/email"


def test_find_sign_in_endpoint_returns_empty_when_absent():
    assert find_sign_in_endpoint(["/api/credits"], []) == ""


def test_newsletter_input_is_not_chosen_as_the_signup_field():
    # A real page carried three email inputs: login, signup, and a footer
    # subscribe box.  The footer's "#susbscribe_email" (sic) won on the id
    # rule and the whole flow filled the wrong field.
    dom = DomInfo(
        inputs=[
            {"type": "email", "id": "", "name": "email", "testid": "",
             "placeholder": "Email Address"},
            {"type": "email", "id": "susbscribe_email", "name": "", "testid": "",
             "placeholder": "Your email address"},
        ],
        buttons=[],
    )
    selectors = infer_selectors(dom)
    assert "susbscribe" not in selectors["email"], selectors
    assert selectors["email"] == 'input[name="email"]', selectors


def test_incidental_input_detection():
    assert looks_incidental({"id": "susbscribe_email", "name": "", "testid": "",
                             "placeholder": ""}) is True
    assert looks_incidental({"id": "", "name": "newsletter", "testid": "",
                             "placeholder": ""}) is True
    assert looks_incidental({"id": "", "name": "email", "testid": "",
                             "placeholder": "Email Address"}) is False


def test_only_incidental_inputs_leaves_no_email_selector():
    # Better to report nothing than to confidently pick the newsletter box.
    dom = DomInfo(
        inputs=[{"type": "email", "id": "susbscribe_email", "name": "", "testid": "",
                 "placeholder": "Your email address"}],
        buttons=[],
    )
    assert "email" not in infer_selectors(dom)


def test_absent_invite_code_is_reported_differently_from_a_bad_one():
    # "no code found" and "code refused" need different actions: the first
    # means the URL may not be an invite link at all, the second means the
    # link is dead.  Reporting both as "did not validate" sends the reader
    # hunting for a dead link that never existed.
    from invitesignup.profile import SiteProfile
    from invitesignup.verdict import BLOCKED_FACTS, decide

    absent = SiteProfile(
        site="example.test",
        base_url="https://example.test",
        invite_url="https://example.test/signup",
        invite_code="",
        invite_valid=None,
        captcha="none",
    )
    v_absent = decide(absent)
    assert v_absent.state == BLOCKED_FACTS
    assert "invite_absent" in v_absent.blockers
    assert any("may not be an invite-gated site" in r for r in v_absent.reasons)

    bad = SiteProfile(
        site="example.test",
        base_url="https://example.test",
        invite_url="https://example.test/invite/DEAD00",
        invite_code="DEAD00",
        invite_valid=False,
        captcha="none",
    )
    v_bad = decide(bad)
    assert "invite_invalid" in v_bad.blockers
    assert any("did not validate" in r for r in v_bad.reasons)


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
