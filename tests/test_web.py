"""Offline tests for the web layer.

No server is started and no browser is launched: these drive the Flask test
client against stubbed-out job targets.  What matters here is the shape of the
API -- that a blocked verdict cannot be talked past, that only one job runs at
a time, and that the job store keeps events in order.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("flask")

from invitesignup.profile import SiteProfile  # noqa: E402
from invitesignup.web import Job, JobStore, create_app  # noqa: E402


def _profile_file(tmp: Path, **overrides) -> Path:
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
    return p.save(tmp / "p.json")


def test_index_serves_the_page():
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(output_dir=tmp)
        client = app.test_client()
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "invite-signup" in body


def test_the_page_does_not_expose_internal_vocabulary():
    """A user hands over a link; "probe", "verdict" and "profile" are our words.

    The page is allowed to use them in code comments, but the visible copy
    must not: someone who has never seen this repo should not have to learn
    that a "verdict" exists.
    """
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(output_dir=tmp)
        body = app.test_client().get("/").get_data(as_text=True)
    # Strip the script block: identifiers there are implementation detail.
    visible = body.split("<script>")[0]
    for word in ("探站", "verdict", "profile", "独立验收", "建号"):
        assert word not in visible, f"internal word {word!r} leaked into the page"


def test_the_page_offers_a_single_entry_point():
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(output_dir=tmp)
        body = app.test_client().get("/").get_data(as_text=True)
    # One URL box and one button, and the copy says so.
    assert 'id="url"' in body
    assert 'id="go"' in body
    assert "把邀请链接贴进来" in body


def test_run_refuses_a_blocked_verdict():
    with tempfile.TemporaryDirectory() as tmp:
        profile = _profile_file(Path(tmp), captcha="turnstile")
        app = create_app(output_dir=tmp)
        resp = app.test_client().post("/api/run", json={"profile": str(profile), "yes": True})
        assert resp.status_code == 400
        payload = resp.get_json()
        assert "verdict" in payload
        assert payload["verdict"]["state"] == "blocked-by-captcha"


def test_run_refuses_without_the_yes_flag():
    with tempfile.TemporaryDirectory() as tmp:
        profile = _profile_file(Path(tmp))
        app = create_app(output_dir=tmp)
        resp = app.test_client().post("/api/run", json={"profile": str(profile)})
        assert resp.status_code == 409
        assert "acknowledgement" in resp.get_json()["error"]


def test_verify_requires_all_three_fields():
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(output_dir=tmp)
        resp = app.test_client().post("/api/verify", json={"profile": "x"})
        assert resp.status_code == 400


def test_probe_requires_a_url():
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(output_dir=tmp)
        resp = app.test_client().post("/api/probe", json={})
        assert resp.status_code == 400


def test_unknown_job_is_404():
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(output_dir=tmp)
        assert app.test_client().get("/api/jobs/nope").status_code == 404


def test_job_store_allows_only_one_running_job():
    store = JobStore()
    started = []

    def slow(job: Job) -> dict:
        started.append(job.id)
        time.sleep(0.4)
        return {"ok": True}

    first = store.start("probe", slow)
    with pytest.raises(RuntimeError):
        store.start("run", slow)
    assert first.status == "running"


def test_job_store_frees_the_slot_after_completion():
    store = JobStore()

    def quick(job: Job) -> dict:
        job.add("stage", {"stage": "x"})
        return {"ok": True}

    store.start("probe", quick)
    deadline = time.time() + 3
    while time.time() < deadline:
        if store.recent()[0]["status"] != "running":
            break
        time.sleep(0.05)
    # A second job must now be accepted.
    second = store.start("probe", quick)
    assert second.status in ("running", "done")


def test_job_records_events_with_elapsed_time():
    job = Job(id="j1", kind="probe")
    job.add("stage", {"stage": "landing"})
    job.add("stage", {"stage": "chunks"})
    assert [e["stage"] for e in job.events] == ["landing", "chunks"]
    assert all("t" in e for e in job.events)


def test_failing_job_is_marked_failed_not_raised():
    store = JobStore()

    def boom(job: Job) -> dict:
        raise ValueError("nope")

    job = store.start("probe", boom)
    deadline = time.time() + 3
    while time.time() < deadline and job.status == "running":
        time.sleep(0.05)
    assert job.status == "failed"
    assert "ValueError" in job.error


def test_profile_name_cannot_escape_the_profiles_directory():
    from invitesignup.web import _safe_profile_name

    for hostile in ("../../etc/passwd", "..\\..\\win.ini", "a/b"):
        try:
            _safe_profile_name(hostile)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{hostile!r} was accepted")


def test_ordinary_profile_names_are_kept():
    from invitesignup.web import _safe_profile_name

    assert _safe_profile_name("yila") == "yila.json"
    assert _safe_profile_name("yila.example") == "yila.example.json"
    assert _safe_profile_name("already.json") == "already.json"
    assert _safe_profile_name("") == ""


def test_a_failed_flow_carries_a_code_and_a_hint():
    """The diagnosis block the user copies must not be missing its code.

    run_flow reports failure in its return value rather than by raising, so
    the web layer has to look at the result.  It did not, and the block the
    user pasted back read "错误类型:" with nothing after it and no suggested
    next step -- exactly the two things the block exists for.
    """
    from invitesignup import web as web_mod

    fake = {
        "ok": False, "stage": "understand", "email": "", "password": "",
        "credits": None, "failure_kind": "wallet",
        "reason": "no form on this page",
        "verdict": {"state": "blocked-wallet-signup", "blockers": ["no_signup_form"]},
    }

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(output_dir=tmp)
        original = web_mod.run_flow
        web_mod.run_flow = lambda *a, **k: dict(fake)
        try:
            resp = app.test_client().post("/api/flow", json={"invite_url": "https://x.test/r/A"})
            job_id = resp.get_json()["job_id"]
            client = app.test_client()
            deadline = time.time() + 5
            payload = {}
            while time.time() < deadline:
                payload = client.get(f"/api/jobs/{job_id}").get_json()
                if payload["status"] != "running":
                    break
                time.sleep(0.05)
        finally:
            web_mod.run_flow = original

    assert payload["status"] == "done"
    assert payload["failure_code"] == "E303"
    assert payload["failure_hint"], "a failure without a hint is just a number"


def test_a_skipped_duplicate_is_not_dressed_up_as_a_failure():
    from invitesignup import web as web_mod

    fake = {"ok": False, "skipped": True, "stage": "skipped-duplicate",
            "failure_kind": "", "reason": "already done today"}

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(output_dir=tmp)
        original = web_mod.run_flow
        web_mod.run_flow = lambda *a, **k: dict(fake)
        try:
            resp = app.test_client().post("/api/flow", json={"invite_url": "https://x.test/r/A"})
            job_id = resp.get_json()["job_id"]
            client = app.test_client()
            deadline = time.time() + 5
            payload = {}
            while time.time() < deadline:
                payload = client.get(f"/api/jobs/{job_id}").get_json()
                if payload["status"] != "running":
                    break
                time.sleep(0.05)
        finally:
            web_mod.run_flow = original

    assert payload["failure_code"] == ""
    assert payload["failure_hint"] == ""


def test_the_inbox_command_reads_back_what_the_runner_stored():
    """End to end on the piece that makes the credentials worth storing.

    Recording a password nobody can use is decoration, so this drives the
    same code path the CLI does: write an account row, then reopen it.
    """
    import io as _io
    from contextlib import redirect_stdout

    from invitesignup import cli
    from invitesignup.mailbox import PROVIDERS, Message, Provider

    class Fake(Provider):
        provider_name = "reopenable"

        def __init__(self, proxy=None, timeout=20):
            self.address = ""

        def credentials(self):
            return {"password": "pw"}

        def reopen(self, creds):
            self.address = creds.get("address", "")
            return self.address

        def messages(self):
            return [Message(id="m1", subject="Reset your password")]

        def body(self, message_id):
            return "click here"

    PROVIDERS["reopenable"] = Fake
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.jsonl"
            path.write_text(
                json.dumps({
                    "site": "example.test",
                    "email": "box@reopenable.test",
                    "password": "acct-pw",
                    "inbox": {"provider": "reopenable",
                              "address": "box@reopenable.test",
                              "password": "pw"},
                }) + "\n",
                encoding="utf-8",
            )
            buf = _io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["inbox", "--email", "box@reopenable.test", "--file", str(path)])
        out = buf.getvalue()
    finally:
        PROVIDERS.pop("reopenable", None)

    assert rc == 0
    assert "box@reopenable.test" in out
    assert "Reset your password" in out


def test_the_inbox_command_explains_a_missing_record():
    import io as _io
    from contextlib import redirect_stdout

    from invitesignup import cli

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "accounts.jsonl"
        path.write_text(
            json.dumps({"email": "box@x.test", "password": "p"}) + "\n",
            encoding="utf-8",
        )
        buf = _io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["inbox", "--email", "box@x.test", "--file", str(path)])
    assert rc == 1
    # "Nothing to reopen" must not look like a crash.
    assert "no stored inbox credentials" in buf.getvalue()


def test_dot_only_names_are_rejected_or_emptied():
    from invitesignup.web import _safe_profile_name

    for name in (".", "..", "..."):
        try:
            assert _safe_profile_name(name) == ""
        except ValueError:
            pass
