"""Site profile: the single source of truth for one invite-gated target site.

A profile is produced by the prober and consumed by the runner and the
verifier.  Everything site-specific lives here; everything shared lives in
the modules that read a profile.

Design rules that came out of real failures:

* Selectors are stored with a *stability class* so the runner can refuse
  fragile ones (see ``SELECTOR_STABILITY``).  Text-based selectors are only
  ever a last resort and must carry the locale they were observed in.
* ``captcha`` and ``verification.mode`` are first-class fields, because they
  decide whether the flow can be automated at all -- not whether it is hard.
* Nothing secret is stored in a profile.  Profiles are meant to be committed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# --- selector stability -------------------------------------------------
# A selector is "stable" if it survives a redesign of the surrounding chrome.
SELECTOR_STABILITY = {
    "id": "stable",              # #invite-password
    "name": "stable",            # input[name=email]
    "data-testid": "stable",     # [data-testid=submit]
    "type": "medium",            # input[type=email]
    "role": "medium",            # [role=button]
    "text": "fragile",           # button:has-text("Continue")  <- locale bound
    "css": "fragile",            # nth-child chains
}

CAPTCHA_KINDS = ("none", "unknown", "turnstile", "recaptcha", "hcaptcha", "slider", "akamai")
VERIFICATION_MODES = ("link", "code", "none", "unknown")


@dataclass
class Attribution:
    """How the invite code is carried into the signup request."""

    kind: str = "cookie"          # cookie | query | header | landing_payload
    name: str = ""                # cookie name / query key / header name
    source: str = ""              # where the code came from (e.g. /invite/<code>)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Verification:
    """How the target proves it sent a verification message."""

    mode: str = "unknown"         # link | code | none | unknown
    sample: str = ""              # example of the URL fragment or code pattern
    pattern: str = ""             # regex used to pull the payload out of a mail body
    resend_endpoint: str = ""     # optional endpoint to re-trigger delivery
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SiteProfile:
    """Everything the pipeline needs to know about one target site."""

    site: str = ""
    base_url: str = ""
    invite_url: str = ""
    invite_code: str = ""

    # what the invite landing told us
    invite_valid: Optional[bool] = None
    reward_credits: Optional[int] = None
    has_reward_slots: Optional[bool] = None
    inviter_name: str = ""

    # referral programme limits, when the site exposes them
    max_successful_invites: Optional[int] = None
    remaining_invites: Optional[int] = None

    attribution: Attribution = field(default_factory=Attribution)
    verification: Verification = field(default_factory=Verification)
    captcha: str = "unknown"

    # the registration steps, in order.  Common values:
    #   email | password | confirm_password | tos | otp | profile
    steps: List[str] = field(default_factory=list)

    # selector -> CSS selector string
    selectors: Dict[str, str] = field(default_factory=dict)
    # selector -> "stable" | "medium" | "fragile"
    selector_stability: Dict[str, str] = field(default_factory=dict)

    # endpoint path -> path
    endpoints: Dict[str, str] = field(default_factory=dict)
    # label -> path, for endpoints that report live account state
    # (credit balance, referral stats, ...).  The verifier reads these too.
    endpoints_state: Dict[str, str] = field(default_factory=dict)
    # how to sign in again from scratch, for independent verification
    sign_in_endpoint: str = "/api/auth/sign-in/email"
    sign_in_fields: Dict[str, str] = field(default_factory=dict)

    # keys used when talking to the endpoints (site specific)
    endpoint_fields: Dict[str, Dict[str, str]] = field(default_factory=dict)

    locale: str = "en-US"
    notes: str = ""
    probed_at: str = ""

    # ---------------- serialisation ----------------

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["attribution"] = self.attribution.to_dict()
        data["verification"] = self.verification.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SiteProfile":
        data = dict(data)
        attribution = data.pop("attribution", None) or {}
        verification = data.pop("verification", None) or {}
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        data = {k: v for k, v in data.items() if k in known}
        return cls(
            attribution=Attribution(**attribution),
            verification=Verification(**verification),
            **data,
        )

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "SiteProfile":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---------------- helpers ----------------

    def stability_of(self, key: str) -> str:
        """Return the stability class of a selector key."""
        if key in self.selector_stability:
            return self.selector_stability[key]
        sel = self.selectors.get(key, "")
        if sel.startswith("#"):
            return SELECTOR_STABILITY["id"]
        if "[name=" in sel:
            return SELECTOR_STABILITY["name"]
        if "data-testid" in sel:
            return SELECTOR_STABILITY["data-testid"]
        if "[type=" in sel:
            return SELECTOR_STABILITY["type"]
        if "has-text" in sel or ":text(" in sel or "text=" in sel:
            return SELECTOR_STABILITY["text"]
        return SELECTOR_STABILITY["css"]

    def fragile_selectors(self) -> Dict[str, str]:
        """Selectors that would break on a copy change or a locale switch."""
        return {k: v for k, v in self.selectors.items() if self.stability_of(k) == "fragile"}

    def missing(self) -> List[str]:
        """Required fields that are still empty."""
        required = ["site", "base_url", "invite_url"]
        return [r for r in required if not getattr(self, r)]
