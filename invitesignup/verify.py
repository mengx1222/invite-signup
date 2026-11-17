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
            endpoint: str = "/api/auth/sign-in/email",
            extra: Optional[Dict[str, Any]] = None,
            timeout: int = 25) -> requests.Session:
    """Sign in and return a session carrying the auth cookies."""
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
    resp = session.post(base_url + endpoint, json=payload, timeout=timeout)
    resp.raise_for_status()
    return session


def fetch_json(session: requests.Session, url: str, timeout: int = 20) -> Any:
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return resp.text


def verify_account(profile, email: str, password: str,
                   sign_in_endpoint: str = "/api/auth/sign-in/email",
                   session_endpoint: str = "/api/auth/get-session",
                   state_endpoints: Optional[Dict[str, str]] = None,
                   timeout: int = 25) -> VerificationReport:
    """Sign in from scratch and confirm the account really exists.

    ``state_endpoints`` maps a label to a path whose payload should carry the
    account's live state (credit balance, referral stats, ...).
    """
    report = VerificationReport()

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
            payload = fetch_json(session, profile.base_url + session_endpoint, timeout=timeout)
            user = payload.get("user") if isinstance(payload, dict) else None
            if user:
                report.session = user
                report.add("session", True, f"user={user.get('email')} verified={user.get('emailVerified')}")
            else:
                report.add("session", False, "no user in the session payload")
                report.reason = "no session"
                return report
        except Exception as exc:
            report.add("session", False, f"{type(exc).__name__}: {exc}")
            report.reason = "no session"
            return report

    for label, path in (state_endpoints or {}).items():
        try:
            payload = fetch_json(session, profile.base_url + path, timeout=timeout)
            report.state[label] = payload
            report.add(f"state:{label}", True, json.dumps(payload, ensure_ascii=False)[:160])
        except Exception as exc:
            report.add(f"state:{label}", False, f"{type(exc).__name__}: {exc}")

    report.verified = all(c["ok"] for c in report.checks if c["name"] == "sign-in") and \
        any(c["ok"] for c in report.checks if c["name"] == "session")
    if not report.verified:
        report.reason = report.reason or "verification did not pass"
    return report
