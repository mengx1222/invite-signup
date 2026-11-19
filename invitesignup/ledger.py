"""Append-only run ledger.

The ledger is the project's answer to "what did we actually do, and can we
prove it?".  It is JSON Lines, append only, one object per run, with a
``dedupe_key`` so the same invite on the same day is never run twice without
a conscious decision.

Rules that are easy to get wrong and are therefore enforced here:

* history is never rewritten -- a retry is a new line with a new attempt_id
* secrets never enter the ledger
* every run has a verdict and an outcome, even when it fails
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

LEDGER_FIELDS = (
    "event_id", "site", "invite_code", "dedupe_key", "attempt_id", "verdict",
    "outcome", "stage", "email", "verified", "reward_credits", "started_at",
    "finished_at", "duration_s", "snapshot", "mail_dump", "last_error", "notes",
)

_SECRET_HINTS = ("password", "token", "secret", "cookie", "api_key", "session")

# One lock per ledger file, shared across every Ledger instance in the
# process.  The web UI opens a new Ledger per request, so an instance-level
# lock would not serialise anything.
_LOCK_REGISTRY: Dict[str, threading.Lock] = {}
_LOCK_REGISTRY_GUARD = threading.Lock()

# Values can carry secrets even when the field name does not say so.
# `last_error` holds a request line, `page_text` holds whatever the site
# printed, and both can contain a verification token or an API key.  The
# ledger is meant to be shareable when debugging, so scrub the values too.
# Each entry pairs a pattern with its replacement.  The first version kept
# two parallel tuples and dispatched on the pattern's index, so inserting a
# new rule silently shifted every replacement onto the wrong pattern.
_VALUE_RULES = (
    # token / key / code as a query parameter
    (re.compile(r"(?i)([?&](?:token|key|api_key|apikey|access_token|code|secret)=)[^&\s\"']+"),
     r"\1<redacted>"),
    # common key prefixes
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{12,}\b"), "<redacted>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
     "<redacted>"),
    # bare "password: xxx" in prose
    (re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+"), r"\1=<redacted>"),
    # "password was hunter2", "password is hunter2"
    (re.compile(r"(?i)\b(password|passwd|pwd)\b(\s+(?:is|was|:|=)\s+)\S+"),
     r"\1\2<redacted>"),
)


def scrub_value(value: str) -> str:
    """Remove secrets that appear *inside* a string, not just in its name."""
    out = value
    for pattern, replacement in _VALUE_RULES:
        out = pattern.sub(replacement, out)
    return out


def scrub_url(url: str) -> str:
    """Keep a request line readable while hiding its credentials.

    ``METHOD /api/auth/verify-email?token=<JWT> -> 200`` is the single most
    useful line when a verification step fails, and it is also a live
    credential.  Scrubbing the query values keeps the path -- which is what
    the reader needs -- and drops the part that must never leave the machine.
    """
    return scrub_value(url)


def dedupe_key(site: str, invite_code: str, day: Optional[str] = None) -> str:
    """Stable business key: one invite code per site per day."""
    day = day or time.strftime("%Y-%m-%d")
    return f"invite-signup:{site}:{invite_code}:{day}"


def scrub(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Strip anything that looks like a secret before it hits disk."""
    out: Dict[str, Any] = {}
    for key, value in payload.items():
        lowered = key.lower()
        if any(hint in lowered for hint in _SECRET_HINTS) and lowered not in ("email",):
            out[key] = "<redacted>"
        elif isinstance(value, str) and len(value) > 24 and "----" in value:
            out[key] = "<redacted>"
        elif isinstance(value, str):
            out[key] = scrub_value(value)
        else:
            out[key] = value
    return out


@dataclass
class Ledger:
    """Append-only JSONL ledger."""

    path: Path
    entries: List[Dict[str, Any]] = field(default_factory=list)

    def _lock_for(self) -> threading.Lock:
        # Measured before this existed: four threads writing 50 rows each
        # produced 197 rows instead of 200 -- three silently lost, because a
        # buffered append is not atomic across threads.
        key = str(self.path.resolve())
        with _LOCK_REGISTRY_GUARD:
            lock = _LOCK_REGISTRY.get(key)
            if lock is None:
                lock = threading.Lock()
                _LOCK_REGISTRY[key] = lock
            return lock

    @classmethod
    def open(cls, path: str | Path) -> "Ledger":
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return cls(path=p)

    # ---------------- reading ----------------

    def read(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if not self.path.exists():
            return rows
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    # A broken line must not stop the rest of the ledger from
                    # being readable.  Skip it and keep going.
                    continue
        return rows

    def find(self, key: str) -> List[Dict[str, Any]]:
        return [r for r in self.read() if r.get("dedupe_key") == key]

    def already_done(self, key: str) -> bool:
        """True when this exact invite/day already produced a successful run."""
        return any(r.get("outcome") == "success" for r in self.find(key))

    # ---------------- writing ----------------

    def append(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        row = scrub(payload)
        row.setdefault("event_id", f"evt-{int(time.time() * 1000)}")
        row.setdefault("attempt_id", "attempt-1")
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self._lock_for():
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        self.entries.append(row)
        return row
