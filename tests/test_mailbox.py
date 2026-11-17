"""Offline tests for the mailbox parsing layer.

These cover the failure that cost the most time in the field: a body field
that is a *list* instead of a string, which used to be swallowed into an
empty body and made a working signup look like a blocked one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitesignup.mailbox import (  # noqa: E402
    extract_code,
    extract_links,
    flatten,
    parse_timestamp,
    pick_link,
)


def test_flatten_string():
    assert flatten("hello") == "hello"


def test_flatten_none():
    assert flatten(None) == ""


def test_flatten_list_of_fragments():
    # This is exactly what mail.tm returns for the `html` field.
    assert flatten(["<p>a</p>", "<p>b</p>"]) == "<p>a</p>\n<p>b</p>"


def test_flatten_nested_list():
    assert flatten([["a", "b"], "c"]) == "a\nb\nc"


def test_flatten_dict():
    assert flatten({"a": "1", "b": "2"}) == "1\n2"


def test_extract_links_finds_and_dedupes():
    body = "see https://yila.ai/api/auth/verify-email?token=abc and https://yila.ai/api/auth/verify-email?token=abc plus https://example.com/x"
    links = extract_links(body)
    assert links.count("https://yila.ai/api/auth/verify-email?token=abc") == 1
    assert "https://example.com/x" in links


def test_extract_links_strips_trailing_punctuation():
    links = extract_links("click https://yila.ai/verify?token=1.")
    assert links == ["https://yila.ai/verify?token=1"]


def test_extract_code_default_pattern():
    assert extract_code("your code is 482913, use it soon") == "482913"


def test_extract_code_custom_pattern():
    assert extract_code("code ABC-1234", r"ABC-\d{4}") == "ABC-1234"


def test_extract_code_returns_none_when_absent():
    assert extract_code("no numbers here") is None


def test_pick_link_prefers_hints():
    links = [
        "https://yila.ai/logo.png",
        "https://yila.ai/api/auth/verify-email?token=abc",
    ]
    assert pick_link(links, ["verify", "token"]) == "https://yila.ai/api/auth/verify-email?token=abc"


def test_pick_link_returns_none_without_hint_match():
    links = ["https://yila.ai/logo.png"]
    assert pick_link(links, ["verify", "token"]) is None


def test_pick_link_without_hints_returns_first():
    assert pick_link(["https://a/1", "https://b/2"], []) == "https://a/1"


def test_parse_timestamp_handles_iso8601():
    # mail.tm sends this shape.  The first version of the code assumed a
    # numeric millisecond string and silently produced 0.0, which disabled
    # the "only accept mail newer than the request" filter entirely.
    value = parse_timestamp("2026-11-17T10:20:06.372Z")
    # 2026-11-17T10:20:06.372Z == 1794910806.372 (UTC)
    assert abs(value - 1_794_910_806.372) < 1.0, value


def test_parse_timestamp_handles_iso8601_without_z():
    assert parse_timestamp("2026-11-17T10:20:06") > 1_700_000_000


def test_parse_timestamp_handles_milliseconds():
    assert abs(parse_timestamp(1_794_910_806_372) - 1_794_910_806.372) < 0.01


def test_parse_timestamp_handles_seconds_and_numeric_string():
    assert parse_timestamp(1_794_910_806) == 1_794_910_806.0
    assert abs(parse_timestamp("1794910806372") - 1_794_910_806.372) < 0.01


def test_parse_timestamp_returns_zero_for_junk():
    assert parse_timestamp(None) == 0.0
    assert parse_timestamp("") == 0.0
    assert parse_timestamp("not a date") == 0.0


def test_poll_skips_messages_in_the_baseline():
    """A message that already existed before the request must be ignored."""
    import time as _time

    from invitesignup.mailbox import Mailbox, Message

    class FakeProvider:
        def __init__(self):
            self.messages_list = [
                Message(id="old", subject="Verify", body="https://x.test/verify?token=OLD"),
            ]

        def create(self):
            return "a@b.test"

        def messages(self):
            return list(self.messages_list)

        def body(self, message_id):
            for m in self.messages_list:
                if m.id == message_id:
                    return m.body
            return ""

    provider = FakeProvider()
    box = Mailbox(provider=provider)
    baseline = box.current_ids()
    assert baseline == {"old"}

    result = box.poll(mode="link", timeout_secs=1, before_ids=baseline)
    assert result.found is False, "the pre-existing message must not count"

    provider.messages_list.append(
        Message(id="new", subject="Verify", body="https://x.test/verify?token=NEW",
                received_at=_time.time())
    )
    result = box.poll(mode="link", timeout_secs=3, before_ids=baseline)
    assert result.found is True
    assert "NEW" in result.payload


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
