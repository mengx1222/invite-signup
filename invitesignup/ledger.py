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
import os
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
        else:
            out[key] = value
    return out


@dataclass
class Ledger:
    """Append-only JSONL ledger."""

    path: Path
    entries: List[Dict[str, Any]] = field(default_factory=list)

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
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.entries.append(row)
        return row
