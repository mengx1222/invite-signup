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

    if not profile.invite_valid:
        v.reasons.append("invite code did not validate")
        v.blockers.append("invite_invalid")
        v.state = BLOCKED_FACTS
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
