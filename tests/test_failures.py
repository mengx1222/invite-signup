"""Offline tests for the failure taxonomy.

The point of this module is one decision: may a failure be retried, or must
the run stop?  These tests pin that decision down, especially the phase flag
that flips a transport error from "retry" to "abort the batch".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitesignup.failures import Action, Kind, classify, should_retry  # noqa: E402


def test_transport_before_commit_is_retryable():
    c = classify("Connection reset by peer", mail_requested=False)
    assert c.kind is Kind.TRANSPORT
    assert c.action is Action.RETRY
    assert c.retryable is True


def test_transport_after_commit_must_abort_the_batch():
    # Once the signup request went out the account may already exist, so a
    # transport error is no longer safe to retry.
    c = classify("Connection reset by peer", mail_requested=True)
    assert c.kind is Kind.TRANSPORT
    assert c.action is Action.ABORT_BATCH
    assert c.retryable is False


def test_rate_limit_before_commit_is_retryable():
    assert classify("HTTP 429 Too Many Requests").action is Action.RETRY


def test_rate_limit_after_commit_stops():
    assert classify("HTTP 429", mail_requested=True).action is Action.STOP


def test_captcha_stops_and_is_not_attempted():
    c = classify("a turnstile challenge is mounted in the live page")
    assert c.kind is Kind.CAPTCHA
    assert c.action is Action.STOP


def test_site_rejection_aborts_the_batch():
    for message in ("USER_ALREADY_EXISTS", "invalid invite code", "HTTP 403 forbidden"):
        c = classify(message)
        assert c.kind is Kind.SITE_REJECTED, message
        assert c.action is Action.ABORT_BATCH, message


def test_mail_timeout_stops_rather_than_retries():
    c = classify("no verification link arrived")
    assert c.kind is Kind.MAIL_TIMEOUT
    assert c.action is Action.STOP


def test_missing_selector_is_a_config_problem():
    c = classify("no password selector in the profile")
    assert c.kind is Kind.CONFIG
    assert c.action is Action.STOP


def test_unclassified_failure_is_not_retried():
    c = classify("something nobody has seen before")
    assert c.kind is Kind.UNKNOWN
    assert c.action is Action.STOP
    assert should_retry("something nobody has seen before") is False


def test_should_retry_wrapper_matches_classify():
    assert should_retry("timeout") is True
    assert should_retry("timeout", mail_requested=True) is False


def test_mailbox_rejection_is_needs_human_not_retry():
    # Retrying with the same address would be refused again; the fix is a
    # different mailbox, which only a person can supply.
    for message in (
        "disposable email addresses are not allowed",
        "Please use a real email address",
        "email domain is not allowed",
    ):
        c = classify(message)
        assert c.kind is Kind.MAILBOX_REJECTED, message
        assert c.action is Action.NEEDS_HUMAN, message
        assert c.retryable is False
        assert c.needs_human is True


def test_mailbox_rejection_beats_generic_site_rejection():
    # "not allowed" also matches the site-rejected signatures, so ordering
    # matters: the more specific kind must win.
    assert classify("disposable domain not allowed").kind is Kind.MAILBOX_REJECTED


def test_chinese_mailbox_rejection_is_recognised():
    assert classify("不支持临时邮箱").kind is Kind.MAILBOX_REJECTED
