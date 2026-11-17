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


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
