"""Decision logic: can this profile be automated, and if not, why.

This module is deliberately pure -- it takes a profile and returns a verdict.
No network, no browser.  That makes the most important safety decision in the
whole pipeline testable offline, and it keeps the "should we stop here?"
question in one place instead of scattered through the runner.

Verdicts
--------
automatable          no captcha, verification is machine-completable, inputs known
needs-human-in-loop  the flow works but one step needs a human (e.g. a real inbox)
blocked-by-captcha    a captcha is present; we do not attempt to defeat one
blocked-by-tos       the site's terms forbid automation; we do not proceed
blocked-missing-facts the probe could not determine something we need
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .profile import SiteProfile

AUTOMATABLE = "automatable"
NEEDS_HUMAN = "needs-human-in-loop"
BLOCKED_CAPTCHA = "blocked-by-captcha"
BLOCKED_TOS = "blocked-by-tos"
BLOCKED_FACTS = "blocked-missing-facts"
BLOCKED_WALLET = "blocked-wallet-signup"


@dataclass
class Verdict:
    state: str = BLOCKED_FACTS
    reasons: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    human_step: str = ""
    # what a caller may do next
    allowed: bool = False
    requires_confirmation: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "reasons": self.reasons,
            "blockers": self.blockers,
            "human_step": self.human_step,
            "allowed": self.allowed,
            "requires_confirmation": self.requires_confirmation,
        }

    def summary(self) -> str:
        head = f"[{self.state}]"
        if self.reasons:
            return head + " " + "; ".join(self.reasons)
        return head


def _captcha_blocks(captcha: str) -> bool:
    """A captcha blocks the run; a mere *suspicion* of one does not."""
    return captcha not in ("", "none", "suspected")


# Endpoint shapes that mean "this site registers a wallet, not a person".
# A signature challenge is not something a form filler can satisfy, so this
# has to be recognised and reported as such rather than as "no invite code".
WALLET_ENDPOINT_MARKERS = (
    "/api/auth/solana/key",
    "/api/auth/wallet",
    "/api/auth/siwe",
    "/api/auth/ethereum",
)


def _looks_wallet_based(profile: SiteProfile) -> bool:
    paths = list((profile.endpoints or {}).values())
    paths += list((profile.endpoints_state or {}).values())
    joined = " ".join(p.lower() for p in paths if isinstance(p, str))
    return any(marker in joined for marker in WALLET_ENDPOINT_MARKERS)


def decide(profile: SiteProfile, tos_forbids_automation: bool = False) -> Verdict:
    """Decide whether a profile may be automated.

    ``tos_forbids_automation`` is an explicit human-supplied fact.  We never
    guess it from marketing copy: an ambiguous Terms of Service is reported,
    not silently treated as permission.
    """
    v = Verdict()

    if tos_forbids_automation:
        v.state = BLOCKED_TOS
        v.blockers.append("site terms forbid automation (declared by the operator)")
        v.reasons.append("terms forbid automation")
        return v

    # A page with no email/password field is not a form-based signup.  Check
    # this before the invite-code checks: on such a site the invite code is
    # irrelevant, and reporting "no invite code found" sends the reader
    # looking for a broken link that is actually fine.
    if getattr(profile, "form_less_signup", False):
        v.state = BLOCKED_WALLET
        v.blockers.append("no_signup_form")
        v.reasons.append(
            "this page has no email or password field, so it does not sign up "
            "through a form (usually a wallet signature or OAuth-only)"
        )
        v.human_step = (
            "register this one by hand in a browser; a form-filling pipeline "
            "cannot and should not hold a wallet key or an OAuth session"
        )
        return v

    if not profile.invite_valid:
        # Distinguish "there was no invite code to check" from "the code was
        # checked and refused".  They need different actions: the first means
        # this may not be an invite site at all, the second means the link is
        # dead.  Reporting both as "did not validate" sends the reader looking
        # for a dead link that never existed.
        if not profile.invite_code:
            v.reasons.append(
                "no invite code was found on this page; "
                "this may not be an invite-gated site"
            )
            v.blockers.append("invite_absent")
            v.human_step = "confirm this URL really is an invite link"
        else:
            v.reasons.append("invite code did not validate")
            v.blockers.append("invite_invalid")
            v.human_step = "check the invite link; it may be expired or mistyped"
        v.state = BLOCKED_FACTS
        return v

    if _looks_wallet_based(profile):
        v.state = BLOCKED_WALLET
        v.blockers.append("wallet_signup")
        v.reasons.append(
            "this site signs up with a crypto wallet, not an email form"
        )
        v.human_step = (
            "this pipeline fills forms; it cannot and should not hold a wallet "
            "private key. Do this one by hand."
        )
        return v

    if _captcha_blocks(profile.captcha):
        v.state = BLOCKED_CAPTCHA
        v.blockers.append(f"captcha:{profile.captcha}")
        v.reasons.append(f"a {profile.captcha} challenge is present and we do not defeat it")
        v.human_step = "solve it yourself in a real browser, or use the site's own UI"
        return v

    if profile.captcha == "suspected":
        # A captcha keyword in the bundles is not proof of a challenge, but it
        # is not proof of its absence either.  Allow the run, insist on a
        # human eye, and let the runner's live-DOM check be the real gate.
        v.reasons.append("captcha keyword found in the bundles but no challenge in the page; "
                         "watch the first run")
        v.requires_confirmation = True
        v.human_step = "keep an eye on the first run; if a challenge appears, solve it by hand"

    if profile.verification.mode == "unknown":
        v.state = BLOCKED_FACTS
        v.blockers.append("verification_mode_unknown")
        v.reasons.append("could not determine whether verification is link-based or code-based")
        v.human_step = "open the verification page manually and read the payload shape"
        return v

    # selectors we cannot do without
    needed = ["email"]
    if "password" in profile.steps:
        needed += ["password"]
    absent = [k for k in needed if not profile.selectors.get(k)]
    if absent:
        v.state = BLOCKED_FACTS
        v.blockers.append("missing_selectors:" + ",".join(absent))
        v.reasons.append("the probe did not find: " + ", ".join(absent))
        v.human_step = "re-probe with advance=True, or fill the selectors by hand"
        return v

    if profile.verification.mode == "code" and not profile.verification.pattern:
        v.state = BLOCKED_FACTS
        v.blockers.append("missing_code_pattern")
        v.reasons.append("verification is code-based but no extraction pattern is known")
        return v

    if profile.verification.mode == "link" and not profile.verification.pattern:
        v.state = BLOCKED_FACTS
        v.blockers.append("missing_link_pattern")
        v.reasons.append("verification is link-based but no extraction pattern is known")
        return v

    fragile = profile.fragile_selectors()
    if fragile:
        v.reasons.append(
            "fragile selectors present (they may break on a redesign or a locale switch): "
            + ", ".join(sorted(fragile))
        )

    if profile.verification.mode == "none":
        v.state = AUTOMATABLE
        v.allowed = True
        v.reasons.append("no email verification step")
        return v

    # Link- and code-based verification both need a mailbox.
    v.state = AUTOMATABLE
    v.allowed = True
    v.requires_confirmation = True
    v.human_step = "if the disposable mailbox is rejected, supply a real inbox and paste the link"
    v.reasons.append(
        f"verification is {profile.verification.mode}-based; a mailbox is required"
    )
    return v
