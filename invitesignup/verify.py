"""Independent verification.

The rule this module exists to enforce: *the executor may not declare
success*.  Verification runs in a separate process, signs in from scratch,
and reports what the server actually says.  A run is only "verified" when
this module says so.

Only HTTP is used -- no browser, no cookies from the run -- so a verification
cannot accidentally succeed because the runner left a session lying around.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests


def _absolute(base_url: str, path: str) -> str:
    """Join a base URL and a path, tolerating an already-absolute path.

    Profiles are hand-edited; writing a full URL where a path was expected is
    easy, and ``base_url + path`` then yields ``https://sitehttps://site/...``
    -- a failure that reads like the site is unreachable.
    """
    if not path:
        return base_url
    if "://" in path:
        return path
    if not path.startswith("/"):
        path = "/" + path
    return base_url.rstrip("/") + path

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


@dataclass
class VerificationReport:
    verified: bool = False
    reason: str = ""
    session: Optional[Dict[str, Any]] = None
    state: Dict[str, Any] = field(default_factory=dict)
    checks: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": ok, "detail": detail})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verified": self.verified,
            "reason": self.reason,
            "session": self.session,
            "state": self.state,
            "checks": self.checks,
        }

    def summary(self) -> str:
        lines = [f"verified={self.verified}"]
        for check in self.checks:
            mark = "ok  " if check["ok"] else "FAIL"
            lines.append(f"  [{mark}] {check['name']}: {check['detail']}")
        return "\n".join(lines)


def sign_in(base_url: str, email: str, password: str,
            endpoint: str = "",
            extra: Optional[Dict[str, Any]] = None,
            timeout: int = 25) -> requests.Session:
    """Sign in and return a session carrying the auth cookies."""
    if not endpoint:
        raise ValueError(
            "no sign-in endpoint: set profile.sign_in_endpoint. "
            "There is no universal default -- /api/auth/sign-in/email is a "
            "Better Auth convention, not a standard."
        )
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Origin": base_url,
        "Referer": base_url + "/sign-in",
        "Accept": "application/json",
    })
    payload: Dict[str, Any] = {"email": email, "password": password, "callbackURL": "/"}
    if extra:
        payload.update(extra)
    resp = session.post(_absolute(base_url, endpoint), json=payload, timeout=timeout)
    resp.raise_for_status()
    return session


def _identity_matches(session_user: Optional[Dict[str, Any]], expected: str) -> tuple:
    """Does the signed-in user match the account we meant to verify?

    Returns ``(ok, detail)``.  When the site does not expose an address at all
    we cannot compare, and saying so is better than claiming a match -- but it
    must not silently count as a pass either.
    """
    if not isinstance(session_user, dict):
        return False, "no user object to compare"
    actual = session_user.get("email") or session_user.get("emailAddress") or ""
    if not actual:
        return False, (
            "the session carries no email address, so the signed-in account "
            "cannot be matched against the one this run created"
        )
    if str(actual).strip().lower() != (expected or "").strip().lower():
        return False, f"signed in as {actual}, expected {expected}"
    return True, f"session belongs to {actual}"


def fetch_json(session: requests.Session, url: str, timeout: int = 20) -> Any:
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return resp.text


def verify_account(profile, email: str, password: str,
                   sign_in_endpoint: str = "",
                   session_endpoint: str = "",
                   state_endpoints: Optional[Dict[str, str]] = None,
                   timeout: int = 25) -> VerificationReport:
    """Sign in from scratch and confirm the account really exists.

    ``state_endpoints`` maps a label to a path whose payload should carry the
    account's live state (credit balance, referral stats, ...).
    """
    report = VerificationReport()

    if not sign_in_endpoint:
        report.add("sign-in", False, "profile has no sign_in_endpoint")
        report.reason = (
            "no sign-in endpoint in the profile; re-probe or add it by hand. "
            "There is no universal default."
        )
        return report

    try:
        session = sign_in(profile.base_url, email, password,
                          endpoint=sign_in_endpoint, timeout=timeout)
        report.add("sign-in", True, f"POST {sign_in_endpoint} accepted")
    except Exception as exc:
        report.add("sign-in", False, f"{type(exc).__name__}: {exc}")
        report.reason = "sign-in failed"
        return report

    if session_endpoint:
        try:
            payload = fetch_json(session, _absolute(profile.base_url, session_endpoint), timeout=timeout)
            user = payload.get("user") if isinstance(payload, dict) else None
            if user:
                report.session = user
                report.add("session", True, f"user={user.get('email')} verified={user.get('emailVerified')}")
            elif isinstance(payload, dict) and (payload.get("email") or payload.get("id")):
                # Some sites return the user object at the top level.
                report.session = payload
                report.add("session", True, f"user={payload.get('email')}")
            else:
                report.add("session", False, "no user in the session payload")
                report.reason = "no session"
                return report

            # A session proves *someone* signed in.  It does not prove it was
            # the account this run created: a reused password, a shared
            # mailbox, or a site that silently falls back to another account
            # would all pass otherwise.  The whole point of verification is
            # that it cannot be satisfied by the wrong account.
            identity_ok, identity_detail = _identity_matches(report.session, email)
            report.add("identity", identity_ok, identity_detail)
            if not identity_ok:
                report.reason = "signed in as a different account"
                return report
        except Exception as exc:
            report.add("session", False, f"{type(exc).__name__}: {exc}")
            report.reason = "no session"
            return report
    else:
        # No session endpoint means there is no user object to compare.  A
        # state endpoint returning 200 only proves *some* credentials work;
        # marking identity as ok here let any working account pass as
        # verified, which defeats the point of this module.
        report.add("session", True, "no session endpoint in profile; relying on state endpoints")
        report.add(
            "identity", False,
            "identity not checkable: the profile has no session endpoint, so "
            "the signed-in account cannot be compared; add "
            "endpoints.session to prove this account",
        )
        report.reason = "identity cannot be verified without a session endpoint"
        return report

    for label, path in (state_endpoints or {}).items():
        try:
            payload = fetch_json(session, _absolute(profile.base_url, path), timeout=timeout)
            report.state[label] = payload
            report.add(f"state:{label}", True, json.dumps(payload, ensure_ascii=False)[:160])
        except Exception as exc:
            report.add(f"state:{label}", False, f"{type(exc).__name__}: {exc}")

    # Identity is required, not optional: without it a verification can be
    # satisfied by any working account on the site.
    report.verified = (
        all(c["ok"] for c in report.checks if c["name"] == "sign-in")
        and any(c["ok"] for c in report.checks if c["name"] == "session")
        and all(c["ok"] for c in report.checks if c["name"] == "identity")
    )
    if not report.verified:
        report.reason = report.reason or "verification did not pass"
    return report
