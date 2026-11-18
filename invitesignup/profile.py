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
from urllib.parse import urlsplit

# Bumped when a field changes meaning or disappears.  Profiles written by an
# older version are read on a best-effort basis and say so, rather than being
# silently misread.
SCHEMA_VERSION = 2

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

    schema_version: int = SCHEMA_VERSION
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
    # How to sign in again from scratch, for independent verification.
    # Deliberately empty by default: `/api/auth/sign-in/email` is a Better Auth
    # convention, and defaulting to it made the whole pipeline quietly
    # Yila-shaped -- it would appear to work only on sites that happen to use
    # that library.  The prober fills this in; a profile without it says so.
    sign_in_endpoint: str = ""
    sign_in_fields: Dict[str, str] = field(default_factory=dict)

    # keys used when talking to the endpoints (site specific)
    endpoint_fields: Dict[str, Dict[str, str]] = field(default_factory=dict)

    # Mailbox providers to try first for this site, in order.  Empty means
    # the default order.  Some sites refuse one provider's domain every time,
    # so re-learning that on each run wastes a mailbox and a wait.
    mailbox_prefer: List[str] = field(default_factory=list)

    locale: str = "en-US"
    notes: str = ""
    probed_at: str = ""

    # Which facts were measured and which were inferred.  A profile that says
    # "selectors: inferred" is a prompt for a human to look, not a defect.
    confidence: Dict[str, str] = field(default_factory=dict)

    # True when the landing page offers no email/password input at all.  Such
    # a site cannot be registered by filling a form -- it is almost always a
    # wallet signature or an OAuth-only flow -- and saying so is far more
    # useful than "no invite code found".
    form_less_signup: bool = False

    # ---------------- serialisation ----------------

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["attribution"] = self.attribution.to_dict()
        data["verification"] = self.verification.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SiteProfile":
        data = dict(data)
        version = data.get("schema_version", 1)
        attribution = data.pop("attribution", None) or {}
        verification = data.pop("verification", None) or {}
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        data = {k: v for k, v in data.items() if k in known}
        profile = cls(
            attribution=Attribution(**attribution),
            verification=Verification(**verification),
            **data,
        )
        if version < SCHEMA_VERSION:
            profile.confidence.setdefault(
                "schema",
                f"written by an older version (v{version}); re-probe to refresh",
            )
        return profile

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

    def selector_report(self) -> List[Dict[str, str]]:
        """The selectors plus their stability, as one list.

        ``selectors`` and ``selector_stability`` are separate dicts, which can
        drift out of sync -- a selector added without its stability entry
        silently falls back to guessing.  This view derives both from one
        place so a caller never sees a half-populated pair.
        """
        return [
            {"key": key, "selector": value, "stability": self.stability_of(key)}
            for key, value in self.selectors.items()
        ]

    def needs_review(self) -> List[str]:
        """Facts a human should confirm before the profile is trusted."""
        reasons: List[str] = []
        for item in self.selector_report():
            if item["stability"] == "fragile":
                reasons.append(f"selector '{item['key']}' is fragile ({item['selector']})")
        if self.verification.mode == "unknown":
            reasons.append("verification mode is unknown")
        if not self.endpoints_state:
            reasons.append("no account-state endpoint (balance will not be reported)")
        if self.confidence.get("selectors") == "inferred":
            reasons.append("selectors were inferred, not confirmed against the live form")
        if not self.sign_in_endpoint:
            reasons.append(
                "no sign-in endpoint: independent verification cannot run until "
                "this is filled in"
            )
        return reasons

    def absolute(self, path: str) -> str:
        """Resolve an endpoint against ``base_url``, tolerating a full URL.

        Profiles are hand-edited, and writing ``"https://site/api/me"`` where
        a path was expected is an easy mistake.  Blindly concatenating
        produced ``"https://sitehttps://site/api/me"`` -- a URL that fails in
        a way that looks like the site is down.  Accept either form.
        """
        if not path:
            return ""
        if urlsplit(path).scheme:
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url.rstrip("/") + path

    def missing(self) -> List[str]:
        """Required fields that are still empty."""
        required = ["site", "base_url", "invite_url"]
        return [r for r in required if not getattr(self, r)]
