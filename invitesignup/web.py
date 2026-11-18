"""A small local web UI.

Design constraints, deliberately narrow:

* **Local only.** Binds 127.0.0.1 by default. This tool holds account
  credentials; exposing it on a network interface is not a convenience
  trade-off, it is a leak.
* **One run at a time.** The runner drives a real browser and a real
  mailbox. Two concurrent runs would fight over the browser and confuse the
  dedupe ledger, so a second start request is refused rather than queued.
* **The core stays untouched.** Everything here goes through the same
  probe/run/verify entry points the CLI uses; the only addition is the
  on_event callback, which lets progress stream instead of appearing after a
  minute of silence.
* **No build step.** One template, inline CSS/JS, no npm.

Start it with:

    python -m invitesignup web
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import ledger as ledger_mod
from . import progress as progress_mod
from .flow import run_flow
from .profile import SiteProfile
from .probe import probe
from .runner import run_registration
from .verdict import decide
from .verify import verify_account


@dataclass
class Job:
    """One in-flight or finished task, with its event history."""

    id: str
    kind: str                      # probe | run | verify
    status: str = "running"        # running | done | failed
    events: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    error: str = ""
    stage: str = ""
    percent: int = 0
    failure_code: str = ""
    failure_hint: str = ""
    # Half-automatic mode: the run parks here until the operator posts a link.
    awaiting_link: bool = False
    supplied_link: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def add(self, kind: str, payload: Dict[str, Any]) -> None:
        entry = {"t": round(time.time() - self.started_at, 2), "kind": kind, **payload}
        self.events.append(entry)
        # Keep the bar in sync with whatever the core reported.  A "waiting"
        # tick does not change the stage, so the percentage stays put while
        # the countdown underneath it moves.
        stage = payload.get("stage")
        if stage and kind in ("stage", "waiting"):
            self.stage = stage
            self.percent = progress_mod.percent(self.kind, stage)
        elif stage and kind == "substage":
            # A step that reports its own progress (bundle scanning), so the
            # bar advances inside the stage instead of sitting still.
            total = payload.get("total") or 0
            done = payload.get("done") or 0
            if total:
                self.stage = stage
                self.percent = progress_mod.percent_within(
                    self.kind, stage, done / total
                )

    def fail(self, kind_name: str) -> None:
        self.failure_code = progress_mod.failure_code(kind_name)
        self.failure_hint = progress_mod.failure_hint(kind_name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "events": self.events,
            "result": self.result,
            "error": self.error,
            "stage": self.stage,
            "stage_label": progress_mod.describe(self.kind, self.stage) if self.stage else "",
            "percent": self.percent,
            "plan": progress_mod.plan(self.kind),
            "failure_code": self.failure_code,
            "failure_hint": self.failure_hint,
            "awaiting_link": self.awaiting_link,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
        }


def _safe_profile_name(name: str) -> str:
    """Turn user input into a filename that cannot leave ``profiles/``.

    Stripping ``/`` and ``\\`` already blocks traversal, but it also turns
    ``"../../etc/passwd"`` into ``"......etcpasswd.json"`` -- a file the user
    did not ask for, sitting in the profiles directory.  Reject anything that
    is not a plain name instead of silently mangling it.
    """
    name = (name or "").strip()
    if not name:
        return ""
    if "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"not a plain file name: {name!r}")
    safe = "".join(c for c in name if c.isalnum() or c in "-_.")
    safe = safe.strip(".")
    if not safe:
        return ""
    if not safe.endswith(".json"):
        safe += ".json"
    return safe


def _kind_for_verdict(verdict) -> str:
    """Map a verdict blocker onto a failure kind, so both share one code table.

    The mapping itself lives on Verdict: keeping a copy here let it drift, so
    the web UI reported a wallet site as a config error while the CLI flow
    reported it correctly.
    """
    return verdict.failure_kind()


def _web_link_prompt(job: "Job"):
    """Block the run until the operator posts a verification link.

    The CLI uses ``input()``; a web request cannot.  The job carries the
    waiting state and a POST to /api/jobs/<id>/link unblocks it.
    """
    def prompt() -> Optional[str]:
        job.awaiting_link = True
        job.add("stage", {"stage": job.stage or "await-verification-link",
                          "message": "waiting for you to paste the verification link"})
        deadline = time.time() + 900
        while time.time() < deadline:
            if job.supplied_link is not None:
                job.awaiting_link = False
                return job.supplied_link or None
            time.sleep(0.4)
        job.awaiting_link = False
        return None
    return prompt


class JobStore:
    """Jobs live in memory.  They are not a database and are not meant to be."""

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._running: Optional[str] = None

    def start(self, kind: str, target) -> Job:
        """Reserve the single execution slot and run ``target`` on a thread."""
        with self._lock:
            if self._running is not None:
                running = self._jobs.get(self._running)
                if running and running.status == "running":
                    raise RuntimeError(
                        f"another {running.kind} job is still running; wait for it to finish"
                    )
            job = Job(id=uuid.uuid4().hex[:12], kind=kind)
            self._jobs[job.id] = job
            self._running = job.id

        def runner() -> None:
            try:
                result = target(job)
                job.result = result
                job.status = "done"
            except Exception as exc:
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = "failed"
                job.add("error", {"message": job.error})
            finally:
                job.finished_at = time.time()

        threading.Thread(target=runner, daemon=True).start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)
        return [j.to_dict() for j in jobs[:limit]]


def create_app(output_dir: str = "./output"):
    """Build the Flask app.  Imported lazily so the CLI works without Flask."""
    from flask import Flask, Response, jsonify, render_template_string, request

    app = Flask(__name__)
    store = JobStore()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    @app.get("/")
    def index():
        return render_template_string(PAGE)

    @app.get("/api/profiles")
    def list_profiles():
        rows = []
        for path in sorted((Path.cwd() / "profiles").glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows.append({
                "name": path.name,
                "path": str(path),
                "site": data.get("site", ""),
                "invite_code": data.get("invite_code", ""),
                "captcha": data.get("captcha", "unknown"),
                "verification": (data.get("verification") or {}).get("mode", "unknown"),
                "reward_credits": data.get("reward_credits"),
            })
        return jsonify(rows)

    @app.get("/api/ledger")
    def get_ledger():
        book = ledger_mod.Ledger.open(out / "ledger.jsonl")
        rows = book.read()[-50:]
        rows.reverse()
        return jsonify(rows)

    @app.get("/api/jobs")
    def list_jobs():
        return jsonify(store.recent())

    @app.get("/api/plan/<kind>")
    def get_plan(kind: str):
        """The stage list a UI draws its checklist from."""
        return jsonify(progress_mod.plan(kind))

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        job = store.get(job_id)
        if job is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(job.to_dict())

    @app.post("/api/jobs/<job_id>/link")
    def post_link(job_id: str):
        """Hand the verification link to a run that is waiting for it."""
        job = store.get(job_id)
        if job is None:
            return jsonify({"error": "not found"}), 404
        if not job.awaiting_link:
            return jsonify({"error": "this job is not waiting for a link"}), 409
        body = request.get_json(force=True) or {}
        link = (body.get("link") or "").strip()
        job.supplied_link = link
        return jsonify({"ok": True})

    @app.get("/api/jobs/<job_id>/stream")
    def stream_job(job_id: str):
        """Server-sent events: one message per new event, then a terminal one."""
        job = store.get(job_id)
        if job is None:
            return jsonify({"error": "not found"}), 404

        def generate():
            sent = 0
            idle = 0.0
            while True:
                while sent < len(job.events):
                    entry = job.events[sent]
                    sent += 1
                    yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                    idle = 0.0
                if job.status != "running":
                    yield "data: " + json.dumps(
                        {"kind": "_end", "status": job.status,
                         "result": job.result, "error": job.error},
                        ensure_ascii=False,
                    ) + "\n\n"
                    return
                time.sleep(0.35)
                idle += 0.35
                if idle > 900:      # 15 min with no events: give up on the stream
                    yield 'data: {"kind":"_end","status":"timeout"}\n\n'
                    return

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/flow")
    def api_flow():
        """One URL in, one account out.  The whole pipeline behind one call.

        The user should not have to know that probing, deciding, registering
        and verifying are separate ideas.  This runs all of them and streams
        one progress line.
        """
        body = request.get_json(force=True) or {}
        url = (body.get("invite_url") or "").strip()
        if not url:
            return jsonify({
                "error": "请先粘贴邀请链接",
                "failure_code": progress_mod.failure_code("config"),
            }), 400

        def target(job: Job) -> Dict[str, Any]:
            result = run_flow(
                url,
                output_dir=out,
                headless=not body.get("headed", False),
                poll_timeout=int(body.get("poll_timeout") or 120),
                link_prompt=_web_link_prompt(job) if body.get("wait_link") else None,
                on_event=lambda kind, payload: job.add(kind, payload),
            )
            # run_flow reports failures in its return value rather than by
            # raising, so nothing set the code or the hint on this path: the
            # diagnosis block the user copies came out with an empty
            # "错误类型" and no suggested next step.  A skipped duplicate is
            # not a failure and must not get one.
            if not result.get("ok") and not result.get("skipped"):
                job.fail(result.get("failure_kind") or "unknown")
            return result

        try:
            job = store.start("flow", target)
        except RuntimeError as exc:
            return jsonify({"error": str(exc),
                            "failure_code": progress_mod.failure_code("config")}), 409
        return jsonify({"job_id": job.id})

    @app.post("/api/probe")
    def api_probe():
        body = request.get_json(force=True) or {}
        url = (body.get("invite_url") or "").strip()
        if not url:
            return jsonify({
                "error": "invite_url is required",
                "failure_code": progress_mod.failure_code("config"),
                "failure_hint": "把邀请链接贴进「邀请链接」输入框。",
            }), 400
        locale = body.get("locale") or "en-US"
        advance = bool(body.get("advance", True))
        tos_blocks = bool(body.get("tos_blocks", False))

        def target(job: Job) -> Dict[str, Any]:
            result = probe(
                url,
                headless=not body.get("headed", False),
                locale=locale,
                advance=advance,
                tos_forbids_automation=tos_blocks,
                on_event=lambda kind, payload: job.add(kind, payload),
            )
            name = (body.get("save_as") or "").strip()
            saved = ""
            if name:
                try:
                    safe = _safe_profile_name(name)
                except ValueError as exc:
                    job.add("error", {"message": str(exc)})
                    safe = ""
                if safe:
                    target = (Path.cwd() / "profiles" / safe).resolve()
                    root = (Path.cwd() / "profiles").resolve()
                    if str(target).startswith(str(root)):
                        saved = str(result.profile.save(target))
            return {
                "profile": result.profile.to_dict(),
                "verdict": result.verdict.to_dict() if result.verdict else None,
                "warnings": result.warnings,
                "saved_as": saved,
            }

        try:
            job = store.start("probe", target)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"job_id": job.id})

    @app.post("/api/run")
    def api_run():
        body = request.get_json(force=True) or {}
        path = (body.get("profile") or "").strip()
        if not path:
            return jsonify({
                "error": "profile is required",
                "failure_code": progress_mod.failure_code("config"),
                "failure_hint": "先在「1 · 探站」跑一次，或在下面的下拉框里选一个已有 profile。",
            }), 400
        try:
            profile = SiteProfile.load(path)
        except Exception as exc:
            return jsonify({"error": f"cannot read profile: {exc}"}), 400

        verdict = decide(profile, tos_forbids_automation=bool(body.get("tos_blocks", False)))
        if not verdict.allowed:
            # Attach a stable code so the UI can show the same panel it uses
            # for failures that happen mid-run, instead of inventing one.
            blocker_kind = _kind_for_verdict(verdict)
            return jsonify({
                "error": "the verdict does not allow a run",
                "verdict": verdict.to_dict(),
                "failure_code": progress_mod.failure_code(blocker_kind),
                "failure_hint": progress_mod.failure_hint(blocker_kind),
            }), 400
        if verdict.requires_confirmation and not body.get("yes"):
            return jsonify({
                "error": "this profile needs an explicit acknowledgement",
                "verdict": verdict.to_dict(),
                "failure_code": progress_mod.failure_code("config"),
                "failure_hint": "先读一遍第 1 步的提示，再勾「我读过提示」重试。",
            }), 409

        def target(job: Job) -> Dict[str, Any]:
            result = run_registration(
                profile,
                output_dir=out,
                email=(body.get("email") or None),
                password=(body.get("password") or None),
                allow_multi=bool(body.get("allow_multi", False)),
                force=bool(body.get("force", False)),
                headless=not body.get("headed", False),
                poll_timeout=int(body.get("poll_timeout") or 120),
                link_prompt=_web_link_prompt(job) if body.get("wait_link") else None,
                on_event=lambda kind, payload: job.add(kind, payload),
            )
            if not result.ok:
                job.fail(result.failure_kind or "unknown")
            return result.to_dict()

        try:
            job = store.start("run", target)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"job_id": job.id})

    @app.post("/api/verify")
    def api_verify():
        body = request.get_json(force=True) or {}
        path = (body.get("profile") or "").strip()
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        if not (path and email and password):
            return jsonify({
                "error": "profile, email and password are required",
                "failure_code": progress_mod.failure_code("config"),
                "failure_hint": "验收要三个都填：profile、建号时用的邮箱、以及那个号的密码。",
            }), 400
        profile = SiteProfile.load(path)

        def target(job: Job) -> Dict[str, Any]:
            job.add("stage", {"stage": "sign-in", "message": "signing in from a clean session"})
            report = verify_account(
                profile,
                email,
                password,
                sign_in_endpoint=profile.sign_in_endpoint,
                session_endpoint=profile.endpoints.get("session", ""),
                state_endpoints=profile.endpoints_state,
            )
            job.add("stage", {"stage": "done", "message": f"verified={report.verified}"})
            return report.to_dict()

        try:
            job = store.start("verify", target)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"job_id": job.id})

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, output_dir: str = "./output") -> None:
    from flask import Flask  # noqa: F401  (fail fast with a clear message)

    app = create_app(output_dir=output_dir)
    print(f"[*] invite-signup UI  ->  http://{host}:{port}")
    print("[*] local only; press Ctrl+C to stop")
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>邀请注册</title>
<style>
  :root {
    --bg: #fbfaf8; --card: #fff; --ink: #24211d; --dim: #6b6560;
    --line: #e6e2dc; --accent: #1c1b19; --ok: #2f7d4f; --warn: #a4691a;
    --bad: #b03a2e; --soft: #f2efe9;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  .wrap { max-width: 640px; margin: 0 auto; padding: 56px 22px 90px; }
  h1 { font-size: 22px; margin: 0 0 6px; letter-spacing: -0.01em; }
  .lead { color: var(--dim); font-size: 14px; margin-bottom: 26px; }

  .card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
          padding: 20px 22px; margin-bottom: 16px; }

  .entry { display: flex; gap: 10px; }
  .entry input {
    flex: 1; min-width: 0; padding: 12px 14px; font: inherit;
    border: 1px solid var(--line); border-radius: 9px; background: #fff; color: var(--ink);
  }
  .entry input:focus { outline: 2px solid #cfc9c0; outline-offset: -1px; }
  .entry button {
    flex: none; padding: 12px 24px; font: inherit; font-weight: 600;
    border: 1px solid var(--accent); border-radius: 9px;
    background: var(--accent); color: #fff; cursor: pointer;
  }
  .entry button:disabled { opacity: .45; cursor: not-allowed; }
  .note { color: var(--dim); font-size: 12.5px; margin-top: 10px; line-height: 1.6; }

  /* the single progress surface */
  .stage-now { font-size: 15px; font-weight: 600; margin-bottom: 3px; }
  .stage-sub { color: var(--dim); font-size: 13px; margin-bottom: 14px; min-height: 20px; }
  .bar-wrap { background: #ece8e2; border-radius: 99px; height: 8px; overflow: hidden; }
  .bar { height: 100%; width: 0; background: var(--accent); border-radius: 99px;
         transition: width .5s ease; }
  .bar.done { background: var(--ok); }
  .bar.fail { background: var(--bad); }
  .pct-row { display: flex; justify-content: space-between; align-items: baseline;
             margin-top: 8px; font-size: 12.5px; color: var(--dim); }
  .pct { font: 600 13px/1 ui-monospace, Consolas, monospace; color: var(--ink); }

  .steps { margin-top: 18px; display: grid; gap: 7px; }
  .step { display: flex; align-items: center; gap: 10px; font-size: 13.5px;
          color: #b3aca4; }
  .step .dot { width: 16px; height: 16px; border-radius: 50%; flex: none;
               border: 1.5px solid #dcd6ce; position: relative; }
  .step.done { color: var(--ink); }
  .step.done .dot { background: var(--ok); border-color: var(--ok); }
  .step.done .dot::after { content: ""; position: absolute; left: 5px; top: 2px;
    width: 4px; height: 8px; border: solid #fff; border-width: 0 1.5px 1.5px 0;
    transform: rotate(45deg); }
  .step.now { color: var(--ink); font-weight: 600; }
  .step.now .dot { border-color: var(--accent); box-shadow: 0 0 0 3px #ece8e2; }
  .step.fail { color: var(--bad); font-weight: 600; }
  .step.fail .dot { border-color: var(--bad); background: #fdf4f2; }
  .step .hint { margin-left: auto; font-size: 12px; color: #b8b1a9; }

  /* result */
  .ok-box { border: 1px solid #cfe4d6; background: #f4faf6; border-radius: 10px;
            padding: 16px 18px; }
  .ok-box .t { font-weight: 700; color: var(--ok); margin-bottom: 10px; }
  .kv { display: grid; grid-template-columns: 62px 1fr; gap: 5px 12px; font-size: 13.5px; }
  .kv .k { color: var(--dim); }
  .kv .v { font-family: ui-monospace, Consolas, monospace; word-break: break-all; }

  /* failure + copyable diagnosis */
  .fail-box { border: 1px solid #efd3cf; background: #fdf4f2; border-radius: 10px;
              padding: 16px 18px; }
  .fail-box .t { font-weight: 700; color: var(--bad); margin-bottom: 8px; }
  .fail-box .why { font-size: 13.5px; color: #6b3b34; line-height: 1.7; }
  .diag { margin-top: 14px; }
  .diag-head { display: flex; align-items: center; gap: 10px; margin-bottom: 7px; }
  .diag-head .lab { font-size: 12.5px; color: var(--dim); }
  .diag-head button {
    padding: 5px 13px; font: inherit; font-size: 12.5px; font-weight: 600;
    border: 1px solid var(--line); border-radius: 7px; background: #fff;
    color: var(--ink); cursor: pointer;
  }
  .diag-head button.copied { border-color: #cfe4d6; color: var(--ok); background: #f4faf6; }
  .diag pre {
    margin: 0; font: 11.5px/1.6 ui-monospace, Consolas, monospace;
    background: #fff; border: 1px solid #f0ded9; border-radius: 7px;
    padding: 11px 13px; max-height: 210px; overflow: auto;
    white-space: pre-wrap; word-break: break-word; color: #6f5a54;
  }

  .log { font: 12px/1.7 ui-monospace, Consolas, monospace; color: #8c857c;
         max-height: 150px; overflow: auto; margin-top: 14px;
         white-space: pre-wrap; word-break: break-word; }
  .muted { color: var(--dim); font-size: 13px; }
  .hidden { display: none !important; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 6px; border-bottom: 1px solid var(--line); }
  th { color: var(--dim); font-weight: 600; font-size: 12px; }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 99px; font-size: 11.5px;
          border: 1px solid var(--line); color: var(--dim); }
  .pill.ok { color: var(--ok); border-color: #cfe4d6; background: #f2f9f4; }
  .pill.bad { color: var(--bad); border-color: #efd3cf; background: #fdf4f2; }
  .pill.warn { color: var(--warn); border-color: #f0e0c6; background: #fdf8ef; }
</style>
</head>
<body>
<div class="wrap">
  <h1>邀请注册</h1>
  <div class="lead">把邀请链接贴进来，点一下。剩下的我来做。</div>

  <div class="card">
    <div class="entry">
      <input id="url" type="text" placeholder="https://.../invite/XXXXXX"
             autocomplete="off" spellcheck="false">
      <button id="go">开始</button>
    </div>
    <div class="note">
      我会自己搞清楚这个站怎么注册、建一个号、再用干净会话验证它真能用。
      中途遇到验证码或站点拒绝会停下并告诉你原因。
    </div>
  </div>

  <div class="card hidden" id="progress-card">
    <div class="stage-now" id="stage-now">准备中</div>
    <div class="stage-sub" id="stage-sub"></div>
    <div class="bar-wrap"><div class="bar" id="bar"></div></div>
    <div class="pct-row"><span id="pct-label">0%</span><span class="pct" id="pct">0%</span></div>
    <div class="steps" id="steps"></div>
    <div class="log hidden" id="log"></div>
  </div>

  <div class="card hidden" id="result-card"></div>

  <div class="card" id="history-card">
    <div class="muted" style="margin-bottom:10px">最近的记录</div>
    <table>
      <thead><tr><th>时间</th><th>站点</th><th>结果</th></tr></thead>
      <tbody id="history"><tr><td colspan="3" class="muted">还没有记录</td></tr></tbody>
    </table>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);

/* The five steps a person actually sees.  Internal stage names never appear. */
const STEPS = [
  { key: "open",     label: "打开邀请链接" },
  { key: "understand", label: "搞清楚这个站怎么注册" },
  { key: "submit",   label: "填写并提交注册" },
  { key: "mail",     label: "等待并确认验证邮件" },
  { key: "verify",   label: "用干净会话验证账号" },
  { key: "save",     label: "保存账号" },
];
const PCT = { open: 6, understand: 22, submit: 45, mail: 62, verify: 88, save: 97, done: 100 };

let currentJob = null, lastRefresh = 0, failStage = "";
let lastStageIdx = -1;

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

function drawSteps(current, failed) {
  const idx = STEPS.findIndex(s => s.key === current);
  const failIdx = failed ? STEPS.findIndex(s => s.key === failed) : -1;
  $("steps").innerHTML = STEPS.map((s, i) => {
    let cls = "step";
    if (failIdx >= 0) {
      // A failed run has a definite stopping point: everything before it
      // succeeded, that step failed, everything after it never ran.  Treating
      // "no current stage" as "all done" (below) turned a failure into a
      // row of green ticks with one red one in the middle.
      if (i < failIdx) cls += " done";
      else if (i === failIdx) cls += " fail";
    } else if (current === "done" || idx === -1) cls += " done";
    else if (i < idx) cls += " done";
    else if (i === idx) cls += " now";
    const mark = failed && s.key === failed ? "出错了" : "";
    return `<div class="${cls}"><span class="dot"></span><span>${esc(s.label)}</span>` +
           `<span class="hint">${mark}</span></div>`;
  }).join("");
}

function setStage(stage, sub) {
  // Progress must never go backwards.  Two phases can legitimately report
  // the same coarse step, and a later phase briefly re-entering an earlier
  // one (the run reopening the page the probe already opened) would make the
  // bar jump back and look like a restart.
  const idx = STEPS.findIndex(s => s.key === stage);
  if (idx >= 0 && idx < lastStageIdx) return;
  if (idx >= 0) lastStageIdx = idx;
  const pct = PCT[stage] !== undefined ? PCT[stage] : 0;
  $("pct").textContent = pct + "%";
  $("pct-label").textContent = pct + "%";
  $("bar").style.width = pct + "%";
  if (sub !== undefined) $("stage-sub").textContent = sub;
  drawSteps(stage, failStage);
}

function logLine(text) {
  const box = $("log");
  box.classList.remove("hidden");
  box.textContent += text + "\n";
  box.scrollTop = box.scrollHeight;
}

function reset() {
  lastStageIdx = -1;
  $("progress-card").classList.remove("hidden");
  $("result-card").classList.add("hidden");
  $("result-card").innerHTML = "";
  $("log").textContent = "";
  $("log").classList.add("hidden");
  $("bar").className = "bar";
  $("stage-now").textContent = "准备中";
  $("stage-sub").textContent = "";
  failStage = "";
  setStage("open", "");
}

function finish() {
  $("go").disabled = false;
  $("go").textContent = "开始";
  currentJob = null;
}

async function poll(jobId) {
  const r = await fetch(`/api/jobs/${jobId}`);
  return r.json();
}

function follow(jobId) {
  currentJob = jobId;
  $("go").disabled = true;
  $("go").textContent = "进行中";
  const es = new EventSource(`/api/jobs/${jobId}/stream`);
  es.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    if (d.kind === "_end") { es.close(); settle(jobId); return; }
    if (d.kind === "stage") {
      $("stage-now").textContent = d.message || (STEPS.find(s => s.key === d.stage) || {}).label || "";
      setStage(d.stage, "");
      logLine(`[${(d.t||0).toFixed(1)}s] ${d.message || d.stage}`);
    } else if (d.kind === "substage") {
      $("stage-sub").textContent = `已检查 ${d.done}/${d.total}`;
      logLine(`[${(d.t||0).toFixed(1)}s] 检查 ${d.done}/${d.total}`);
    } else if (d.kind === "waiting") {
      $("stage-sub").textContent = `等待中，还剩 ${d.remaining} 秒`;
    } else if (d.kind === "verdict") {
      logLine(`[${(d.t||0).toFixed(1)}s] 判定：${(d.verdict||{}).state}`);
    } else if (d.kind === "error") {
      logLine(`[${(d.t||0).toFixed(1)}s] 错误：${d.message}`);
    }
  };
  es.onerror = () => { es.close(); settle(jobId); };
}

async function settle(jobId) {
  const j = await poll(jobId);
  finish();
  const r = j.result || {};
  if (r.ok) {
    $("bar").className = "bar done";
    setStage("done", "");
    $("stage-now").textContent = "完成";
    showSuccess(r);
  } else if (r.skipped) {
    // "Already done today" is not a failure: nothing went wrong, the guard
    // simply refused to create a second account from the same link.
    $("bar").className = "bar";
    $("bar").style.width = "100%";
    $("pct").textContent = "—";
    $("pct-label").textContent = "已跳过";
    $("stage-now").textContent = "今天已经做过";
    $("stage-sub").textContent = "";
    // Only the steps that actually ran get ticked.  Marking all six as done
    // implied the account was created and verified, which is the opposite of
    // what a skip means.
    drawSteps("submit", "");
    showSkipped(r);
  } else {
    $("bar").className = "bar fail";
    failStage = mapFailStage(r.stage);
    drawSteps("", failStage);
    // Freeze the bar where the run actually stopped, not where the last
    // event happened to leave it.
    const stoppedAt = PCT[failStage] !== undefined ? PCT[failStage] : 0;
    $("bar").style.width = stoppedAt + "%";
    $("pct").textContent = stoppedAt + "%";
    $("pct-label").textContent = "已停止";
    // The result card already says "没有完成" in full; repeating it here
    // made the page look like two separate errors.
    $("stage-now").textContent = "已停止";
    $("stage-sub").textContent = r.reason || "";
    showFailure(r, j);
  }
  refreshHistory();
}

/* Map a failing internal stage onto the step the user was watching. */
function mapFailStage(stage) {
  if (!stage) return "understand";
  if (stage.startsWith("await")) return "mail";
  if (stage === "confirm-link") return "mail";
  if (stage === "confirm" || stage === "state") return "verify";
  if (stage === "mailbox-rejected" || stage === "mailbox-failed") return "submit";
  if (stage === "email" || stage === "password" || stage === "submit") return "submit";
  if (stage === "landing") return "open";
  return "understand";
}

function showSuccess(r) {
  const box = $("result-card");
  box.classList.remove("hidden");
  // A null balance means "the site does not expose it to an unauthenticated
  // probe", not "zero".  Saying "—" made people think the account had no
  // credits.
  const credits = (r.credits !== null && r.credits !== undefined)
    ? r.credits : "未读到（登录后可见）";
  box.innerHTML = `
    <div class="ok-box">
      <div class="t">账号已建好并验证通过</div>
      <div class="kv">
        <span class="k">邮箱</span><span class="v">${esc(r.email || "")}</span>
        <span class="k">密码</span><span class="v">${esc(r.password || "")}</span>
        <span class="k">额度</span><span class="v">${esc(credits)}</span>
      </div>
    </div>`;
}

function showSkipped(r) {
  const box = $("result-card");
  box.classList.remove("hidden");
  box.innerHTML = `
    <div class="ok-box" style="border-color:#f0e0c6;background:#fdf8ef">
      <div class="t" style="color:var(--warn)">今天已经用这个链接成功注册过</div>
      <div class="why" style="font-size:13.5px;color:#6b5a3b">
        为防重复建号，这次没有再跑一遍。同一个邀请码每天只建一个号。
        如果你确实想再建一个，把这段诊断发我。
      </div>
      <div class="diag">
        <div class="diag-head">
          <span class="lab">诊断</span>
          <button id="copy-btn">复制</button>
        </div>
        <pre id="diag-text">${esc(buildDiagnosis(r, {}))}</pre>
      </div>
    </div>`;
  wireCopy(buildDiagnosis(r, {}));
}

function wireCopy(text) {
  $("copy-btn").onclick = async () => {
    try { await navigator.clipboard.writeText(text); }
    catch (e) {
      const pre = $("diag-text");
      const range = document.createRange();
      range.selectNodeContents(pre);
      const sel = window.getSelection();
      sel.removeAllRanges(); sel.addRange(range);
      document.execCommand("copy");
    }
    const b = $("copy-btn");
    b.textContent = "已复制"; b.classList.add("copied");
    setTimeout(() => { b.textContent = "复制"; b.classList.remove("copied"); }, 1800);
  };
}

function buildDiagnosis(r, j) {
  const lines = [
    "=== invite-signup 诊断 ===",
    `时间: ${new Date().toISOString()}`,
    `链接: ${$("url").value}`,
    `阶段: ${r.stage || ""}`,
    `错误类型: ${[r.failure_kind, j.failure_code].filter(Boolean).join(" / ")}`,
    `原因: ${r.reason || j.error || ""}`,
  ];
  if (r.verdict) lines.push(`判定: ${JSON.stringify(r.verdict)}`);
  if (r.profile) {
    const p = r.profile;
    lines.push(`站点: ${p.site} captcha=${p.captcha} verification=${(p.verification||{}).mode}`);
    lines.push(`选择器: ${JSON.stringify(p.selectors || {})}`);
    lines.push(`端点: ${JSON.stringify(p.endpoints || {})}`);
  }
  if (r.detail && r.detail.length) {
    lines.push("", "--- 详细错误 ---", ...r.detail);
  }
  if (r.page_text) lines.push("", "--- 页面提示 ---", r.page_text);
  if (r.trace && r.trace.length) {
    lines.push("", "--- 请求轨迹 ---", ...r.trace.slice(-25));
  }
  if (j && j.failure_hint) lines.push("", `建议: ${j.failure_hint}`);
  return lines.join("\n");
}

function showFailure(r, j) {
  const box = $("result-card");
  box.classList.remove("hidden");
  const diag = buildDiagnosis(r, j);
  box.innerHTML = `
    <div class="fail-box">
      <div class="t">没有完成</div>
      <div class="why">${esc(r.reason || j.error || "未知原因")}</div>
      <div class="diag">
        <div class="diag-head">
          <span class="lab">把下面这段复制发给我，我来修</span>
          <button id="copy-btn">复制</button>
        </div>
        <pre id="diag-text">${esc(diag)}</pre>
      </div>
    </div>`;
  wireCopy(diag);
}

async function start() {
  const url = $("url").value.trim();
  if (!url) { $("url").focus(); return; }
  reset();
  const resp = await fetch("/api/flow", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ invite_url: url }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    $("go").disabled = false;
    $("bar").className = "bar fail";
    failStage = "open";
    drawSteps("", "open");
    $("stage-now").textContent = "没有开始";
    showFailure({ reason: data.error || `HTTP ${resp.status}`, trace: [] }, {});
    return;
  }
  follow(data.job_id);
}

$("go").onclick = start;
$("url").addEventListener("keydown", e => { if (e.key === "Enter") start(); });

async function refreshHistory() {
  const rows = await (await fetch("/api/ledger")).json();
  const body = $("history");
  if (!rows.length) { body.innerHTML = '<tr><td colspan="3" class="muted">还没有记录</td></tr>'; return; }
  body.innerHTML = rows.slice(0, 8).map(r => {
    const kind = r.outcome === "success" ? "ok"
               : (r.outcome === "skipped" || r.outcome === "declined") ? "warn" : "bad";
    const label = r.outcome === "success" ? "成功"
                : r.outcome === "skipped" ? "已跳过"
                : r.outcome === "declined" ? "未开始" : "失败";
    const cls = kind;
    return `<tr><td class="muted">${esc((r.started_at||"").replace("T"," ").slice(5,16))}</td>` +
           `<td>${esc(r.site||"")}</td>` +
           `<td><span class="pill ${cls}">${label}</span></td></tr>`;
  }).join("");
}

refreshHistory();
</script>
</body>
</html>
"""
