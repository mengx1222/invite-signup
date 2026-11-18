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

import invitesignup.mailbox  # noqa: E402
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


def _fake_provider(name, *, restorable=True, messages=()):
    """A provider that behaves like a real one but talks to nothing."""

    class Fake(Provider):
        provider_name = name

        def __init__(self, proxy=None, timeout=20):
            self.address = ""

        def create(self):
            self.address = f"box@{name}.test"
            return self.address

        def credentials(self):
            return {"password": "pw-123"} if restorable else {}

        def reopen(self, creds):
            self.address = creds.get("address", "")
            return self.address

        def messages(self):
            return list(messages)

        def body(self, message_id):
            return "body"

    return Fake


def test_an_inbox_can_be_reopened_from_its_saved_credentials():
    """Storing an address without its password makes the inbox write-only.

    A site that later sends a password reset has nowhere readable to send it.
    """
    Fake = _fake_provider("restorable")
    PROVIDERS["restorable"] = Fake
    try:
        box = Mailbox()
        address = box.create(order=["restorable"])
        saved = box.export()

        assert saved["address"] == address
        assert saved["provider"] == "restorable"
        assert saved["password"] == "pw-123"

        again = Mailbox()
        assert again.reopen(saved) == address
    finally:
        PROVIDERS.pop("restorable", None)


def test_mail_tm_keeps_the_password_it_created_the_inbox_with():
    """The real provider, with the network stubbed out.

    The original bug was a local variable: `password = _random_password()`
    was used to create the account and then dropped when create() returned,
    so the address could never be opened again.  Testing a fake provider
    cannot catch that -- the password has to be thrown away in *this* class
    for the test to mean anything.
    """
    import types

    from invitesignup.mailbox import MailTmProvider

    seen = {}

    class Resp:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        return Resp({"hydra:member": [{"domain": "example.test"}]})

    def fake_post(url, json=None, **kw):
        if url.endswith("/accounts"):
            seen["created_password"] = json["password"]
            return Resp({"id": "1"})
        if url.endswith("/token"):
            seen["login_password"] = json["password"]
            return Resp({"token": "tok"})
        raise AssertionError(url)

    fake = types.SimpleNamespace(get=fake_get, post=fake_post)
    original = invitesignup.mailbox.requests
    invitesignup.mailbox.requests = fake
    try:
        provider = MailTmProvider()
        address = provider.create()
        creds = provider.credentials()
    finally:
        invitesignup.mailbox.requests = original

    assert address.endswith("@example.test")
    assert creds.get("password"), "the inbox password was discarded"
    assert creds["password"] == seen["created_password"]
    assert seen["login_password"] == seen["created_password"]


def test_a_provider_that_cannot_be_reopened_exports_nothing():
    """Returning a bare address would look restorable and fail later."""
    Fake = _fake_provider("one-shot", restorable=False)
    PROVIDERS["one-shot"] = Fake
    try:
        box = Mailbox()
        box.create(order=["one-shot"])
        assert box.export() == {}
    finally:
        PROVIDERS.pop("one-shot", None)


def test_reopening_an_unknown_provider_says_so():
    from invitesignup.mailbox import NoProviderAvailable

    try:
        Mailbox().reopen({"provider": "gone-forever", "address": "a@b.test"})
    except NoProviderAvailable as exc:
        assert "gone-forever" in str(exc)
    else:
        raise AssertionError("an unknown provider was accepted")


def test_an_operator_supplied_mailbox_exports_nothing():
    """When the address is the operator's own, there is nothing to store."""
    box = Mailbox()
    assert box.export() == {}


def test_a_profile_can_record_which_provider_this_site_accepts():
    """A site that refuses one domain every time should not cost a failed run
    on every attempt.  The preference is data on the profile, so re-running
    is enough -- no code change, no new flag to remember.
    """
    from invitesignup.profile import SiteProfile

    p = SiteProfile(site="example.test", mailbox_prefer=["pref-b"])
    assert p.to_dict()["mailbox_prefer"] == ["pref-b"]

    restored = SiteProfile.from_dict(p.to_dict())
    assert restored.mailbox_prefer == ["pref-b"]

    # A profile written before this field existed must still load.
    old = SiteProfile(site="legacy.test").to_dict()
    old.pop("mailbox_prefer")
    assert SiteProfile.from_dict(old).mailbox_prefer == []


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
