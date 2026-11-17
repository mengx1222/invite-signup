"""Every exit path must leave a ledger row.

The first version of run_registration() returned early from about a dozen
guard clauses -- blocked by verdict, duplicate, quota, mailbox failure, and
every step failure.  All of those returns skipped the ledger write at the
bottom of the function, so the ledger silently under-counted attempts and the
dedupe/quota logic had nothing to read.

These tests pin the invariant without needing a browser: each case fails
before the browser is ever launched, which is exactly the path that used to
skip persistence.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitesignup.profile import SiteProfile  # noqa: E402
from invitesignup.runner import run_registration  # noqa: E402


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
    p.verification.pattern = r"https://example\.test/verify\?token="
    for key, value in overrides.items():
        setattr(p, key, value)
    return p


def _rows(tmp: str):
    path = Path(tmp) / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_blocked_by_verdict_still_writes_a_row():
    with tempfile.TemporaryDirectory() as tmp:
        profile = _profile(captcha="turnstile")
        result = run_registration(profile, output_dir=tmp)
        assert result.ok is False
        assert result.stage == "blocked-by-verdict"

        rows = _rows(tmp)
        assert len(rows) == 1, f"expected one ledger row, got {len(rows)}"
        assert rows[0]["outcome"] == "failure"
        assert rows[0]["stage"] == "blocked-by-verdict"


def test_blocked_by_quota_still_writes_a_row():
    with tempfile.TemporaryDirectory() as tmp:
        profile = _profile()
        profile.max_successful_invites = 1

        ledger = Path(tmp) / "ledger.jsonl"
        ledger.write_text(json.dumps({
            "dedupe_key": "invite-signup:example.test:CODE1:" + __import__("time").strftime("%Y-%m-%d"),
            "outcome": "success",
            "stage": "state",
        }) + "\n", encoding="utf-8")

        # force=True skips the dedupe gate so the quota gate is the one that
        # fires; otherwise the duplicate check short-circuits first.
        result = run_registration(profile, output_dir=tmp, force=True)
        assert result.stage == "blocked-by-quota"

        rows = _rows(tmp)
        assert len(rows) == 2, "the blocked attempt must be appended, not swallowed"
        assert rows[-1]["stage"] == "blocked-by-quota"


def test_duplicate_skip_still_writes_a_row():
    with tempfile.TemporaryDirectory() as tmp:
        profile = _profile()
        ledger = Path(tmp) / "ledger.jsonl"
        ledger.write_text(json.dumps({
            "dedupe_key": "invite-signup:example.test:CODE1:" + __import__("time").strftime("%Y-%m-%d"),
            "outcome": "success",
            "stage": "state",
        }) + "\n", encoding="utf-8")

        result = run_registration(profile, output_dir=tmp)
        assert result.stage == "skipped-duplicate"

        rows = _rows(tmp)
        assert len(rows) == 2
        assert rows[-1]["stage"] == "skipped-duplicate"


def test_missing_selector_still_writes_a_row():
    # No email selector: the run dies at the email step, before a browser is
    # even opened.  That path used to leave no trace.
    with tempfile.TemporaryDirectory() as tmp:
        profile = _profile()
        profile.selectors.pop("email")
        result = run_registration(profile, output_dir=tmp)
        assert result.ok is False

        rows = _rows(tmp)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "failure"


def test_failure_gets_a_classification():
    with tempfile.TemporaryDirectory() as tmp:
        profile = _profile(captcha="turnstile")
        result = run_registration(profile, output_dir=tmp)
        assert result.failure_kind == "captcha"
        assert result.failure_action == "stop"
