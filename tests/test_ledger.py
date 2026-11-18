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


def test_secrets_inside_values_are_scrubbed():
    # The field name does not always say "secret": last_error holds a request
    # line and page_text holds whatever the site printed.  Both can carry a
    # verification token or an API key.
    from invitesignup.ledger import scrub_value

    cases = [
        "GET /verify?token=eyJhbGciOiJIUzI1NiJ9.abcdefghij.klmnopqrst",
        "POST /auth?api_key=sk-live-1234567890abcdef",
        "your key is sk-proj-ABCDEFGHIJKLMNOP",
        "gho_abcdefghijklmnopqrstuvwx",
        "password was hunter2",
    ]
    for text in cases:
        cleaned = scrub_value(text)
        assert "<redacted>" in cleaned, f"nothing scrubbed in {text!r} -> {cleaned!r}"


def test_scrubbing_leaves_ordinary_text_alone():
    from invitesignup.ledger import scrub_value

    for text in (
        "no session after the flow",
        "the site refused this mailbox: disposable",
        "mail_dump_2026-11-18.txt",
    ):
        assert scrub_value(text) == text


def test_ledger_row_keeps_email_but_scrubs_a_token_in_notes():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.jsonl"
        book = Ledger.open(path)
        book.append({
            "dedupe_key": "k",
            "email": "a@b.c",
            "notes": "link https://x/verify?token=eyJhbGciOiJIUzI1NiJ9.aa.bb",
        })
        raw = path.read_text(encoding="utf-8")
        assert "a@b.c" in raw
        assert "eyJhbGciOiJIUzI1NiJ9" not in raw


def test_concurrent_appends_do_not_lose_rows():
    # The web UI opens a fresh Ledger per request, so several instances can
    # write the same file.  Before the per-path lock existed, four threads
    # writing 50 rows each produced 197 rows instead of 200.
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.jsonl"
        book = Ledger.open(path)

        def write(worker: int) -> None:
            for i in range(50):
                book.append({"dedupe_key": f"k{worker}-{i}", "outcome": "failure"})

        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 200, f"lost rows: {len(lines)}/200"
        for line in lines:
            json.loads(line)  # every line must still be valid JSON


def test_two_ledger_instances_on_one_file_share_the_lock():
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.jsonl"
        a, b = Ledger.open(path), Ledger.open(path)

        def write(book, worker: int) -> None:
            for i in range(40):
                book.append({"dedupe_key": f"w{worker}-{i}", "outcome": "failure"})

        threads = [
            threading.Thread(target=write, args=(a, 0)),
            threading.Thread(target=write, args=(b, 1)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 80, f"lost rows: {len(lines)}/80"


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
