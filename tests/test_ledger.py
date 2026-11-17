"""Offline tests for the append-only ledger."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitesignup.ledger import Ledger, dedupe_key  # noqa: E402


def test_append_and_read_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        book = Ledger.open(Path(tmp) / "ledger.jsonl")
        book.append({"dedupe_key": "k1", "outcome": "success", "email": "a@b.c"})
        rows = book.read()
        assert len(rows) == 1
        assert rows[0]["outcome"] == "success"


def test_history_is_never_rewritten():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.jsonl"
        book = Ledger.open(path)
        book.append({"dedupe_key": "k1", "outcome": "failure", "stage": "email"})
        book.append({"dedupe_key": "k1", "outcome": "success", "stage": "confirm"})
        rows = book.read()
        assert len(rows) == 2
        assert rows[0]["outcome"] == "failure"
        assert rows[1]["outcome"] == "success"


def test_already_done_only_counts_success():
    with tempfile.TemporaryDirectory() as tmp:
        book = Ledger.open(Path(tmp) / "ledger.jsonl")
        key = dedupe_key("site", "CODE")
        book.append({"dedupe_key": key, "outcome": "failure"})
        assert book.already_done(key) is False
        book.append({"dedupe_key": key, "outcome": "success"})
        assert book.already_done(key) is True


def test_broken_line_does_not_stop_the_reader():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.jsonl"
        path.write_text('{"ok":1}\n{broken\n{"ok":2}\n', encoding="utf-8")
        book = Ledger.open(path)
        rows = book.read()
        assert len(rows) == 2


def test_secrets_are_scrubbed_before_write():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.jsonl"
        book = Ledger.open(path)
        book.append({"dedupe_key": "k", "password": "hunter2", "email": "a@b.c"})
        raw = path.read_text(encoding="utf-8")
        assert "hunter2" not in raw
        assert "a@b.c" in raw


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
