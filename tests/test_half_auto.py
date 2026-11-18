"""Tests for --wait-link and the early mailbox-rejection check.

Both exist because the most common real-world snag is "the site will not
accept a disposable address".  Previously that surfaced as a two-minute wait
followed by "no verification link arrived", which says nothing about the
actual cause.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitesignup.mailbox import PROVIDERS, Mailbox, Provider  # noqa: E402
from invitesignup.probe import TrafficLog  # noqa: E402
from invitesignup.runner import _mailbox_was_rejected  # noqa: E402


def test_rejection_phrases_are_recognised():
    for text in (
        "Disposable email addresses are not allowed",
        "Please use a real email",
        "email domain is not allowed",
        "不支持临时邮箱",
    ):
        assert _mailbox_was_rejected(text) is True, text


def test_ordinary_error_text_is_not_a_rejection():
    for text in (
        "Invalid password",
        "This field is required",
        "",
    ):
        assert _mailbox_was_rejected(text) is False, text


def test_traffic_log_accepts_non_api_prefixes():
    # Hardcoding /api/ silently drops /v1/, /graphql and custom auth routes.
    log = TrafficLog()
    log.add("POST", "https://example.test/v1/signup")
    log.add("POST", "https://example.test/graphql")
    log.add("GET", "https://example.test/auth/session")
    assert "/v1/signup" in log.paths
    assert "/graphql" in log.paths
    assert "/auth/session" in log.paths


def test_traffic_log_prefixes_are_overridable():
    log = TrafficLog(prefixes=("/custom/",))
    log.add("GET", "https://example.test/custom/ping")
    log.add("GET", "https://example.test/api/ignored")
    assert log.paths == ["/custom/ping"]


def test_create_preferring_puts_the_preferred_provider_first():
    seen = []

    class Fake(Provider):
        def __init__(self, proxy=None, timeout=20):
            pass

        def create(self):
            seen.append(self.provider_name)
            return f"x@{self.provider_name}.test"

        def messages(self):
            return []

        def body(self, message_id):
            return ""

    for name in ("pref-a", "pref-b"):
        PROVIDERS[name] = type(name, (Fake,), {"provider_name": name})
    try:
        box = Mailbox()
        address = box.create_preferring(preferred=["pref-b", "pref-a"])
        assert address.endswith("@pref-b.test")
        assert seen == ["pref-b"]
    finally:
        for name in ("pref-a", "pref-b"):
            PROVIDERS.pop(name, None)
