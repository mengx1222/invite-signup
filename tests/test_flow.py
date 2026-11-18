"""Tests for the one-click flow's translation and failure reporting.

The flow exists so a user hands over one URL.  What matters here is that the
internal stage vocabulary never leaks out, that a forbidden verdict stops the
flow instead of being worked around, and that a failure carries enough detail
to be copied and acted on.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitesignup import flow  # noqa: E402
from invitesignup.profile import SiteProfile  # noqa: E402
from invitesignup.verdict import Verdict  # noqa: E402


def _verdict(state: str, blockers, reasons, allowed: bool) -> Verdict:
    return Verdict(state=state, blockers=list(blockers), reasons=list(reasons),
                   allowed=allowed, human_step="")


def test_probe_stages_map_onto_user_visible_steps():
    seen = []
    probe_sink, _ = flow._translator(lambda k, p: seen.append((k, p)))

    for internal in ("landing", "chunks", "config", "dom", "advance"):
        probe_sink("stage", {"stage": internal})

    steps = [p["stage"] for k, p in seen if k == "stage"]
    assert steps == ["open", "understand", "understand", "understand", "understand"]
    # No internal vocabulary may reach the listener.
    for step in steps:
        assert step not in ("chunks", "config", "dom", "advance", "landing")


def test_run_stages_map_onto_user_visible_steps():
    seen = []
    _, run_sink = flow._translator(lambda k, p: seen.append((k, p)))

    run_sink("stage", {"stage": "email"})
    run_sink("stage", {"stage": "password"})
    run_sink("stage", {"stage": "await-verification-link"})
    run_sink("stage", {"stage": "confirm"})

    steps = [p["stage"] for k, p in seen]
    assert steps == ["submit", "submit", "mail", "verify"]


def test_substage_keeps_its_counts():
    seen = []
    probe_sink, _ = flow._translator(lambda k, p: seen.append((k, p)))
    probe_sink("substage", {"stage": "chunks", "done": 7, "total": 25})
    assert seen[0][0] == "substage"
    assert seen[0][1]["done"] == 7 and seen[0][1]["total"] == 25


def test_captcha_verdict_stops_the_flow_and_says_why():
    v = _verdict("blocked-by-captcha", ["captcha:turnstile"],
                 ["a turnstile challenge is present"], allowed=False)
    assert flow._kind_for(v) == "captcha"
    reason = flow._plain_reason(v)
    assert "验证码" in reason
    assert "不会绕过" in reason


def test_absent_invite_is_explained_in_plain_words():
    v = _verdict("blocked-missing-facts", ["invite_absent"],
                 ["no invite code was found"], allowed=False)
    reason = flow._plain_reason(v)
    assert "邀请码" in reason
    assert "不是邀请" in reason


def test_mailbox_rejection_asks_for_a_real_inbox():
    class _Result:
        stage = "mailbox-rejected"
        errors = ["the site refused this mailbox: disposable"]
    text = flow._plain_failure(_Result())
    assert "一次性邮箱" in text
    assert "发我" in text  # tells the user what to do with the diagnosis


def test_mail_timeout_is_distinguished_from_rejection():
    class _Result:
        stage = "await-verification-link"
        errors = ["no verification link arrived"]
    text = flow._plain_failure(_Result())
    assert "没等到验证邮件" in text
    assert "一次性邮箱" not in text


def test_balance_is_found_under_several_key_names():
    for payload in (
        {"data": {"balance": 500}},
        {"data": {"available": 120}},
        {"credits": 42},
        {"data": {"quota": 9}},
    ):
        assert flow._balance({"credits": payload}) is not None


def test_balance_is_none_when_the_site_reports_nothing():
    assert flow._balance({}) is None
    assert flow._balance({"x": {"data": {"note": "hello"}}}) is None
