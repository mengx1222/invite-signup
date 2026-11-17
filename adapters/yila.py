"""Yila AI -- the first verified target site.

Everything in this file was confirmed against the live site on 2026-11-17 by
running the flow end to end.  It is kept as a worked example and as a
regression target: if a future change to the pipeline breaks this site, the
example profile stops matching.

No credentials and no live invite code live here -- the example profile uses
placeholders.

Protocol summary
----------------
landing      GET  /en/invite/<code>      server sets cookie ``yila_invite_code``
                                      and renders ``landingData``:
                                      {valid, referralCode, inviterName,
                                       rewardCredits, hasRewardSlots}
existence    POST /api/referral/check-email  {email, referralCode}
                                      -> {code:0, data:{exists:bool}}
sign-up      POST /api/auth/sign-up/email   {name, email, password}
                                      -> 200 {user:{emailVerified:false}}
send mail    POST /api/auth/send-verification-email
verify       GET  /api/auth/verify-email?token=<JWT>&callbackURL=/research
                                      -> 302; then /api/auth/get-session
                                      returns a user with emailVerified:true
session      GET  /api/auth/get-session
credits      GET  /api/credits              -> {data:{balance, ...}}
referral     GET  /api/referral/me          -> {rewardCredits,
                                      maxSuccessfulInvites, remainingInvites, ...}
sign in      POST /api/auth/sign-in/email   {email, password}

Notable properties
------------------
* no captcha of any kind
* verification is **link based** -- /verify-email has no code input, the page
  polls get-session once a second until the account flips to verified
* there is **no standalone sign-up page**: /en/sign-up redirects to
  /en/sign-in, and the registration form only exists on the invite landing
  page.  "Register without an invite" does not exist in the UI.
* the referral programme caps at ``maxSuccessfulInvites`` (4 when observed)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitesignup.profile import Attribution, SiteProfile, Verification  # noqa: E402

EXAMPLE_INVITE_CODE = "YL-EXAMPLE00"


def build_profile(invite_code: str = EXAMPLE_INVITE_CODE) -> SiteProfile:
    """Return the hand-verified profile for Yila AI."""
    return SiteProfile(
        site="yila.ai",
        base_url="https://yila.ai",
        invite_url=f"https://yila.ai/en/invite/{invite_code}",
        invite_code=invite_code,
        invite_valid=True,
        reward_credits=200,
        has_reward_slots=True,
        attribution=Attribution(
            kind="cookie",
            name="yila_invite_code",
            source="server sets the cookie when the invite landing page is fetched",
        ),
        verification=Verification(
            mode="link",
            sample="/api/auth/verify-email?token=",
            pattern=r"https?://[^\s\"'<>\\]*yila\.ai/api/auth/verify-email[^\s\"'<>\\]*",
            notes="the mail carries a link; /verify-email has no code input and polls get-session",
        ),
        captcha="none",
        steps=["email", "password"],
        selectors={
            "email": 'input[type="email"]',
            "password": "#invite-password",
            "confirm_password": "#invite-confirm-password",
            "submit": 'button[type="submit"]',
        },
        endpoints={
            "check-email": "/api/referral/check-email",
            "sign-up": "/api/auth/sign-up/email",
            "send-verification": "/api/auth/send-verification-email",
            "verify": "/api/auth/verify-email",
            "session": "/api/auth/get-session",
            "credits": "/api/credits",
            "referral": "/api/referral/me",
        },
        endpoints_state={
            "credits": "/api/credits",
            "referral": "/api/referral/me",
        },
        sign_in_endpoint="/api/auth/sign-in/email",
        endpoint_fields={
            "check-email": {"email": "email", "referralCode": "referralCode"},
            "sign-up": {"email": "email", "password": "password"},
            "sign-in": {"email": "email", "password": "password"},
        },
        locale="en-US",
        notes=(
            "Verified end to end on 2026-11-17: 27.9 s, no captcha, link-based "
            "verification, 500 credits granted as a single signup_bonus grant. "
            "Registration is only reachable through an invite landing page."
        ),
        probed_at="2026-11-17",
    )


def main() -> int:
    out = Path(__file__).resolve().parent.parent / "profiles" / "yila.example.json"
    path = build_profile().save(out)
    print(f"[+] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
