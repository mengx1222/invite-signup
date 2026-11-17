"""The verdict's "needs a human ack" flag must actually gate the run.

``requires_confirmation`` was computed and then never read by anything --
a field that looks like a safety gate but gates nothing.  These tests pin the
behaviour: without ``--yes`` the CLI refuses and exits 3; with it, the run
proceeds.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitesignup.cli import main  # noqa: E402
from invitesignup.profile import SiteProfile  # noqa: E402


def _write_profile(path: Path, **overrides) -> Path:
    p = SiteProfile(
        site="example.test",
        base_url="https://example.test",
        invite_url="https://example.test/invite/CODE1",
        invite_code="CODE1",
        invite_valid=True,
        captcha="none",
        steps=["email", "password"],
        selectors={"email": 'input[type="email"]', "password": "#pw"},
    )
    p.verification.mode = "link"
    p.verification.pattern = r"https://example\.test/verify\?token="
    for key, value in overrides.items():
        setattr(p, key, value)
    p.save(path)
    return path


def test_run_refuses_without_yes_when_confirmation_is_required():
    with tempfile.TemporaryDirectory() as tmp:
        profile = _write_profile(Path(tmp) / "p.json")
        code = main(["run", "--profile", str(profile), "--output-dir", tmp])
        assert code == 3, f"expected the gate to return 3, got {code}"

        # The decline itself is recorded.  Without this row, "we never tried"
        # and "we tried and the gate stopped it" look identical in the ledger.
        ledger = Path(tmp) / "ledger.jsonl"
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(rows) == 1
        assert rows[0]["outcome"] == "declined"
        assert rows[0]["stage"] == "gate-no-yes"


def test_run_proceeds_past_the_gate_with_yes():
    with tempfile.TemporaryDirectory() as tmp:
        # A captcha blocks regardless of --yes, so use a clean profile and
        # check that we get past the gate (the run then fails on its own
        # merits because there is no browser work to do here).
        profile = _write_profile(Path(tmp) / "p.json")
        code = main(["run", "--profile", str(profile), "--output-dir", tmp, "--yes"])
        assert code != 3, "with --yes the gate must not fire"


def test_blocked_verdict_still_wins_over_yes():
    with tempfile.TemporaryDirectory() as tmp:
        profile = _write_profile(Path(tmp) / "p.json", captcha="turnstile")
        code = main(["run", "--profile", str(profile), "--output-dir", tmp, "--yes"])
        # `run` reports a failed run as 1; 2 is `probe`'s "verdict disallows"
        # code and 3 is the confirmation gate.  What matters here is that
        # --yes cannot talk its way past a captcha.
        assert code == 1, f"a captcha must still block even with --yes, got {code}"
