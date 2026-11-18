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
    """Map a verdict blocker onto a failure kind, so both share one code table."""
    blockers = getattr(verdict, "blockers", []) or []
    joined = " ".join(blockers).lower()
    if "captcha" in joined:
        return "captcha"
    if "tos" in joined:
        return "site_rejected"
    if "quota" in joined:
        return "site_rejected"
    return "config"


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
<title>invite-signup</title>
<style>
  :root {
    --bg: #fbfaf8; --card: #fff; --ink: #24211d; --dim: #6b6560;
    --line: #e6e2dc; --accent: #1c1b19; --ok: #2f7d4f; --warn: #a4691a; --bad: #b03a2e;
    --tip: #f2efe9;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  .wrap { max-width: 880px; margin: 0 auto; padding: 30px 20px 90px; }
  h1 { font-size: 21px; margin: 0 0 3px; letter-spacing: -0.01em; }
  .sub { color: var(--dim); font-size: 13px; margin-bottom: 20px; }

  /* what this tool is, for a first-time visitor */
  .intro { background: var(--tip); border-radius: 10px; padding: 15px 18px; margin-bottom: 18px;
           font-size: 13.5px; line-height: 1.7; color: #4a453f; }
  .intro b { color: var(--ink); }
  .intro .flow { font-family: ui-monospace, Consolas, monospace; font-size: 12.5px;
                 background: #fff; border: 1px solid var(--line); border-radius: 6px;
                 padding: 9px 12px; margin: 9px 0 0; overflow-x: auto; white-space: nowrap; }

  .card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
          padding: 17px 20px; margin-bottom: 14px; }
  .card > h2 { font-size: 13px; margin: 0 0 4px; color: var(--dim); font-weight: 600;
               letter-spacing: .05em; text-transform: uppercase; }
  .card > .why { font-size: 13px; color: var(--dim); margin: 0 0 14px; line-height: 1.6; }

  label { display: block; font-size: 12.5px; color: var(--dim); margin: 11px 0 4px; }
  input[type=text], input[type=password], select {
    width: 100%; padding: 9px 11px; border: 1px solid var(--line); border-radius: 7px;
    font: inherit; background: #fff; color: var(--ink);
  }
  input:focus, select:focus { outline: 2px solid #cfc9c0; outline-offset: -1px; }
  .row { display: flex; gap: 10px; }
  .row > * { flex: 1; min-width: 0; }
  button {
    font: inherit; font-weight: 600; padding: 10px 18px; border-radius: 7px;
    border: 1px solid var(--accent); background: var(--accent); color: #fff; cursor: pointer;
  }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.ghost { background: #fff; color: var(--ink); border-color: var(--line); }
  .chk { display: flex; align-items: flex-start; gap: 7px; margin-top: 9px;
         font-size: 13px; color: var(--dim); }
  .chk input { width: auto; margin-top: 3px; flex: none; }
  .chk .n { display: block; color: #9b948c; font-size: 12px; margin-top: 1px; }

  /* progress */
  .bar-wrap { background: #ece8e2; border-radius: 99px; height: 10px; overflow: hidden; }
  .bar { height: 100%; width: 0%; background: var(--accent); border-radius: 99px;
         transition: width .45s ease; }
  .bar.done { background: var(--ok); }
  .bar.fail { background: var(--bad); }
  .pct { font: 600 26px/1 ui-monospace, Consolas, monospace; letter-spacing: -0.02em; }
  .pct small { font-size: 13px; color: var(--dim); font-weight: 500; margin-left: 6px;
               font-family: inherit; letter-spacing: 0; }
  .prog-head { display: flex; align-items: baseline; justify-content: space-between;
               margin-bottom: 9px; gap: 12px; }
  .stage-now { font-size: 13px; color: var(--dim); }
  .steps { margin-top: 14px; display: grid; gap: 3px; }
  .step { display: flex; align-items: center; gap: 9px; font-size: 13px;
          color: #a9a29a; padding: 2px 0; }
  .step .dot { width: 14px; height: 14px; border-radius: 50%; border: 1.5px solid #d8d2ca;
               flex: none; position: relative; }
  .step.done { color: var(--ink); }
  .step.done .dot { background: var(--ok); border-color: var(--ok); }
  .step.done .dot::after { content: ""; position: absolute; left: 4px; top: 1px;
    width: 4px; height: 8px; border: solid #fff; border-width: 0 1.5px 1.5px 0;
    transform: rotate(45deg); }
  .step.now { color: var(--ink); font-weight: 600; }
  .step.now .dot { border-color: var(--accent); box-shadow: 0 0 0 3px #ece8e2; }
  .step .p { margin-left: auto; font-size: 11.5px; color: #b8b1a9;
             font-family: ui-monospace, Consolas, monospace; }

  .log {
    font: 12.5px/1.7 ui-monospace, "Cascadia Mono", Consolas, monospace;
    background: #1c1b19; color: #e8e4de; border-radius: 8px; padding: 13px 15px;
    max-height: 300px; overflow: auto; white-space: pre-wrap; word-break: break-word;
  }
  .log .t { color: #8c857c; }
  .log .k-stage { color: #8ec6a8; }
  .log .k-error { color: #e08a80; }
  .log .k-verdict { color: #d9b36c; }
  .log .k-waiting { color: #9aa8c6; }
  .log .k-substage { color: #9aa8c6; }
  .log .k-finished { color: #8ec6a8; font-weight: 600; }

  .pill { display: inline-block; padding: 2px 9px; border-radius: 99px; font-size: 12px;
          border: 1px solid var(--line); color: var(--dim); }
  .pill.ok { color: var(--ok); border-color: #cfe4d6; background: #f2f9f4; }
  .pill.bad { color: var(--bad); border-color: #efd3cf; background: #fdf4f2; }
  .pill.warn { color: var(--warn); border-color: #f0e0c6; background: #fdf8ef; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--line); }
  th { color: var(--dim); font-weight: 600; font-size: 12px; }
  .muted { color: var(--dim); font-size: 13px; }
  .err { color: var(--bad); font-size: 13px; margin-top: 9px; white-space: pre-wrap; }
  .hint { color: var(--dim); font-size: 12.5px; margin-top: 9px; line-height: 1.6; }

  /* failure panel: code + what to do */
  .fail { border: 1px solid #efd3cf; background: #fdf4f2; border-radius: 9px;
          padding: 14px 16px; margin-top: 13px; }
  .fail .code { font: 700 13px/1 ui-monospace, Consolas, monospace; color: var(--bad);
                letter-spacing: .04em; }
  .fail .msg { font-size: 13.5px; margin-top: 7px; color: #6b3b34; }
  .fail .todo { font-size: 13px; margin-top: 8px; color: #4a453f;
                border-top: 1px dashed #e3c7c1; padding-top: 8px; }
  .fail .raw { font: 11.5px/1.6 ui-monospace, Consolas, monospace; color: #8a6f68;
               margin-top: 9px; max-height: 130px; overflow: auto;
               background: #fff; border: 1px solid #f0ded9; border-radius: 6px; padding: 8px 10px; }
  .await { border: 1px solid #e0d5bf; background: #fdf8ef; border-radius: 9px;
           padding: 14px 16px; margin-top: 13px; }
  .await .t2 { font-weight: 600; font-size: 13.5px; margin-bottom: 7px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>invite-signup</h1>
  <div class="sub">邀请制注册流水线 · 本机运行 · 一次只跑一个任务</div>

  <div class="intro">
    <b>这个工具做什么</b>：给一个邀请链接，它先搞清楚这个站怎么注册，再按你的确认建一个号，
    最后用干净会话验证账号真的能用。<b>站点不限于某一个</b>——换站点只需要重新探站，
    站点差异都记在 profile 里。
    <div class="flow">① 探站（只读） → ② 判定能不能跑 → ③ 建一个号 → ④ 独立验收</div>
  </div>

  <div class="card">
    <h2>1 · 探站</h2>
    <p class="why">把一个邀请链接丢进来。它只读，<b>不会建号</b>——唯一的写操作是一次
      「这个邮箱注册过吗」的检查。跑完会告诉你这站能不能自动注册。</p>
    <label>邀请链接</label>
    <input id="invite_url" type="text" placeholder="https://example.com/invite/CODE">
    <div class="row">
      <div>
        <label>保存为（留空则不保存）</label>
        <input id="save_as" type="text" placeholder="example">
      </div>
      <div>
        <label>语言</label>
        <select id="locale">
          <option value="en-US">en-US</option>
          <option value="zh-CN">zh-CN</option>
        </select>
      </div>
    </div>
    <div class="chk"><input id="headed_probe" type="checkbox">
      <span>显示浏览器窗口<span class="n">想亲眼看它点了什么就勾上</span></span></div>
    <div class="chk"><input id="no_advance" type="checkbox">
      <span>不做存在性检查<span class="n">更保守，但看不到第二步的密码框</span></span></div>
    <div class="chk"><input id="tos_blocks" type="checkbox">
      <span>这个站的条款禁止自动化<span class="n">勾上会直接判定为不可跑，流程停在这里</span></span></div>
    <div style="margin-top:15px"><button id="btn_probe">开始探站</button></div>
    <div id="probe_err" class="err"></div>
  </div>

  <div class="card">
    <h2>2 · 建号</h2>
    <p class="why"><b>这一步在做什么</b>：拿第 1 步生成的 profile，按里面的规则自动打开邀请页、
      填邮箱密码、提交、然后去收验证邮件并点开里面的链接，最后确认账号真的登录上了。
      默认<b>只建 1 个号</b>，用一次性邮箱，全程不用你管。</p>
    <label>Profile（第 1 步的产物）</label>
    <select id="profile"><option value="">— 先探站，或选一个已有的 —</option></select>
    <div id="profile_meta" class="hint"></div>
    <div class="row">
      <div>
        <label>邮箱<span class="n" style="display:inline"> · 留空则用一次性邮箱</span></label>
        <input id="run_email" type="text" placeholder="留空 = 自动">
      </div>
      <div>
        <label>密码<span class="n" style="display:inline"> · 留空则自动生成</span></label>
        <input id="run_password" type="password" placeholder="留空 = 自动">
      </div>
    </div>
    <div class="row">
      <div>
        <label>等信超时（秒）</label>
        <input id="poll_timeout" type="text" value="120">
      </div>
    </div>
    <div class="chk"><input id="run_yes" type="checkbox">
      <span>我读过第 1 步的提示，可以开始<span class="n">判定允许但带附加条件时，必须勾这个</span></span></div>
    <div class="chk"><input id="run_wait_link" type="checkbox">
      <span>用我自己的邮箱，收到信我自己贴链接<span class="n">站点拒一次性邮箱时用这个；只有「贴链接」一步是人工的</span></span></div>
    <div class="chk"><input id="run_headed" type="checkbox">
      <span>显示浏览器窗口</span></div>
    <div class="chk"><input id="run_force" type="checkbox">
      <span>忽略今天的去重记录<span class="n">同一个邀请码今天已经成功过，才需要勾</span></span></div>
    <div class="chk"><input id="run_multi" type="checkbox">
      <span>允许超出邀请配额<span class="n">这是保护不是限制，想清楚再勾</span></span></div>
    <div style="margin-top:15px"><button id="btn_run">开始建号</button></div>
    <div id="run_err" class="err"></div>
  </div>

  <div class="card">
    <h2>3 · 独立验收</h2>
    <p class="why">建号脚本说成功<b>不算数</b>。这一步换一个干净会话重新登录，只看服务端怎么回答。
      只有这里说 verified=true，那个号才算真的能用。</p>
    <div class="row">
      <div><label>邮箱</label><input id="ver_email" type="text"></div>
      <div><label>密码</label><input id="ver_password" type="password"></div>
    </div>
    <div style="margin-top:15px"><button id="btn_verify" class="ghost">从干净会话验证</button></div>
    <div id="ver_err" class="err"></div>
  </div>

  <div class="card">
    <h2>进度</h2>
    <div class="prog-head">
      <div class="pct"><span id="pct">0</span><small>%</small></div>
      <div class="stage-now" id="stage_now">空闲</div>
    </div>
    <div class="bar-wrap"><div class="bar" id="bar"></div></div>
    <div class="steps" id="steps"></div>

    <div id="await_box" class="await" style="display:none">
      <div class="t2">需要你贴一下验证链接</div>
      <div class="muted" style="margin-bottom:9px">信已经发到你的邮箱了。打开它，复制里面的验证链接，粘在下面。</div>
      <div class="row">
        <input id="link_input" type="text" placeholder="https://.../verify?token=...">
        <button id="btn_link" style="flex:none">提交链接</button>
      </div>
    </div>

    <div id="fail_box" class="fail" style="display:none">
      <div class="code" id="fail_code"></div>
      <div class="msg" id="fail_msg"></div>
      <div class="todo" id="fail_todo"></div>
      <div class="raw" id="fail_raw"></div>
    </div>

    <div style="margin-top:15px"><div id="log" class="log">—</div></div>
  </div>

  <div class="card">
    <h2>台账</h2>
    <table>
      <thead><tr><th>时间</th><th>站点</th><th>结果</th><th>阶段</th><th>邮箱</th></tr></thead>
      <tbody id="ledger"><tr><td colspan="5" class="muted">暂无记录</td></tr></tbody>
    </table>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let currentJob = null, plan = [], planKind = "", lastRefresh = 0;

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

/* ---------- progress ---------- */
function renderPlan(kind) {
  planKind = kind;
  fetch(`/api/plan/${kind}`).then(r => r.json()).then(p => { plan = p; drawSteps("", 0); });
}
function drawSteps(currentStage, percent) {
  const box = $("steps");
  if (!plan.length) { box.innerHTML = ""; return; }
  const idx = plan.findIndex(s => s.stage === currentStage);
  box.innerHTML = plan.map((s, i) => {
    let cls = "step";
    if (idx >= 0 && i < idx) cls += " done";
    else if (idx >= 0 && i === idx) cls += " now";
    return `<div class="${cls}"><span class="dot"></span><span>${esc(s.label)}</span>` +
           `<span class="p">${s.percent}%</span></div>`;
  }).join("");
}
function setProgress(percent, stage, label) {
  $("pct").textContent = percent;
  $("bar").style.width = percent + "%";
  $("bar").className = "bar";
  if (stage) { $("stage_now").textContent = label || stage; drawSteps(stage, percent); }
}

/* ---------- log ---------- */
function logLine(e) {
  const box = $("log");
  if (box.textContent.trim() === "—") box.textContent = "";
  const t = document.createElement("div");
  const stamp = e.t !== undefined ? `[${e.t.toFixed(1)}s] ` : "";
  t.innerHTML = `<span class="t">${stamp}</span>` +
    `<span class="k-${e.kind}">${esc(e.kind)}</span> ${esc(describe(e))}`;
  box.appendChild(t);
  box.scrollTop = box.scrollHeight;
}
function describe(e) {
  if (e.kind === "stage") return `${e.stage}${e.message ? " — " + e.message : ""}`;
  if (e.kind === "waiting") return `已等 ${e.elapsed}s / 共 ${e.total}s，还剩 ${e.remaining}s`;
  if (e.kind === "substage") return e.message || `${e.done}/${e.total}`;
  if (e.kind === "verdict") {
    const v = e.verdict || {};
    return `[${v.state}] ${(v.reasons || []).join("; ")}`;
  }
  if (e.kind === "invite") {
    const p = e.payload || {};
    return `邀请码有效=${p.valid} 奖励=${p.rewardCredits}`;
  }
  if (e.kind === "identity") return `邮箱 ${e.email}`;
  if (e.kind === "error") return e.message || "error";
  if (e.kind === "finished") {
    const r = e.result || {};
    return `ok=${r.ok} verified=${r.verified}`;
  }
  return e.message || JSON.stringify(e);
}

/* ---------- failure panel ---------- */
function showFailure(code, msg, hint, raw) {
  if (!code) { $("fail_box").style.display = "none"; return; }
  $("fail_box").style.display = "";
  $("fail_code").textContent = code;
  $("fail_msg").textContent = msg || "";
  $("fail_todo").textContent = hint ? "下一步：" + hint : "";
  $("fail_raw").textContent = raw || "";
  $("fail_raw").style.display = raw ? "" : "none";
}

function setBusy(busy, label) {
  ["btn_probe","btn_run","btn_verify"].forEach(id => { $(id).disabled = busy; });
  if (busy && label) $("stage_now").textContent = label;
  if (!busy && !currentJob) $("stage_now").textContent = "空闲";
}

function follow(jobId, kind, label) {
  currentJob = jobId;
  $("log").textContent = "";
  $("fail_box").style.display = "none";
  $("await_box").style.display = "none";
  $("bar").className = "bar";
  renderPlan(kind);
  setBusy(true, label);
  const es = new EventSource(`/api/jobs/${jobId}/stream`);
  es.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    if (d.kind === "_end") { es.close(); finish(jobId); return; }
    logLine(d);
    if (d.kind === "stage" || d.kind === "waiting" || d.kind === "substage") {
      // A bundle scan emits a substage event per bundle; fetching the job on
      // each one hammered the server.  Coalesce to at most one refresh a
      // second -- the bar does not need to move faster than that.
      const now = Date.now();
      if (!lastRefresh || now - lastRefresh > 900) {
        lastRefresh = now;
        fetch(`/api/jobs/${jobId}`).then(r => r.json()).then(j => {
          setProgress(j.percent, j.stage, j.stage_label);
          $("await_box").style.display = j.awaiting_link ? "" : "none";
        });
      }
    }
  };
  es.onerror = () => { es.close(); finish(jobId); };
}

function finish(jobId) {
  setBusy(false);
  fetch(`/api/jobs/${jobId}`).then(r => r.json()).then(j => {
    currentJob = null;
    if (j.status === "failed") {
      $("bar").className = "bar fail";
      showFailure(j.failure_code || "E900", j.error, j.failure_hint, j.error);
    } else {
      const r = j.result || {};
      if (r.verdict) {
        const v = r.verdict;
        setProgress(100, "", "");
        $("bar").className = v.allowed ? "bar done" : "bar fail";
        const cls = v.allowed ? (v.state === "automatable" ? "ok" : "warn") : "bad";
        $("stage_now").innerHTML = `<span class="pill ${cls}">${esc(v.state)}</span> ` +
          esc((v.reasons || []).join("; "));
        if (r.saved_as) $("stage_now").innerHTML += ` <span class="muted">→ ${esc(r.saved_as)}</span>`;
        if (!v.allowed) showFailure("E400", (v.reasons||[]).join("; "), v.human_step || "");
      } else if (r.ok !== undefined) {
        $("bar").className = r.ok ? "bar done" : "bar fail";
        setProgress(r.ok ? 100 : j.percent, j.stage, j.stage_label);
        $("stage_now").innerHTML = `<span class="pill ${r.ok?'ok':'bad'}">ok=${r.ok} verified=${r.verified}</span>`;
        if (r.email) $("ver_email").value = r.email;
        if (r.password) $("ver_password").value = r.password;
        if (!r.ok) {
          const raw = (r.errors || []).join("\n");
          showFailure(j.failure_code || "E900", (r.errors||[])[0] || "", j.failure_hint, raw);
        }
      } else if (r.verified !== undefined) {
        $("bar").className = r.verified ? "bar done" : "bar fail";
        setProgress(100, "", "");
        $("stage_now").innerHTML = `<span class="pill ${r.verified?'ok':'bad'}">verified=${r.verified}</span>`;
        if (!r.verified) showFailure("E302", r.reason || "", j.failure_hint,
          JSON.stringify(r.checks || [], null, 1));
      }
    }
    refreshLedger(); refreshProfiles();
  });
}

/* ---------- api ---------- */
async function post(url, body, errId) {
  $(errId).textContent = "";
  const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"},
                             body: JSON.stringify(body)});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    let msg = data.error || `HTTP ${r.status}`;
    if (data.verdict) {
      msg += "\n" + (data.verdict.reasons || []).join("\n");
      if (data.verdict.human_step) msg += "\n→ " + data.verdict.human_step;
    }
    $(errId).textContent = msg;
    // Show the same failure panel used mid-run, so a rejection before the job
    // starts looks like every other failure instead of a bare sentence.
    showFailure(data.failure_code || "E400", data.error || msg,
                data.failure_hint || "",
                data.verdict ? JSON.stringify(data.verdict, null, 1) : "");
    return null;
  }
  return data;
}

$("btn_probe").onclick = async () => {
  const d = await post("/api/probe", {
    invite_url: $("invite_url").value, save_as: $("save_as").value,
    locale: $("locale").value, headed: $("headed_probe").checked,
    advance: !$("no_advance").checked, tos_blocks: $("tos_blocks").checked,
  }, "probe_err");
  if (d) follow(d.job_id, "probe", "探站中…");
};

$("btn_run").onclick = async () => {
  const d = await post("/api/run", {
    profile: $("profile").value, email: $("run_email").value,
    password: $("run_password").value,
    poll_timeout: parseInt($("poll_timeout").value || "120", 10),
    headed: $("run_headed").checked, yes: $("run_yes").checked,
    force: $("run_force").checked, allow_multi: $("run_multi").checked,
    wait_link: $("run_wait_link").checked,
  }, "run_err");
  if (d) follow(d.job_id, "run", "建号中…");
};

$("btn_verify").onclick = async () => {
  const d = await post("/api/verify", {
    profile: $("profile").value,
    email: $("ver_email").value, password: $("ver_password").value,
  }, "ver_err");
  if (d) follow(d.job_id, "verify", "验收中…");
};

$("btn_link").onclick = async () => {
  if (!currentJob) return;
  const link = $("link_input").value.trim();
  const r = await fetch(`/api/jobs/${currentJob}/link`, {
    method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({link})
  });
  if (r.ok) { $("await_box").style.display = "none"; $("link_input").value = ""; }
};

/* ---------- lists ---------- */
async function refreshProfiles() {
  const rows = await (await fetch("/api/profiles")).json();
  const sel = $("profile"), keep = sel.value;
  sel.innerHTML = '<option value="">— 选择一个 profile —</option>';
  rows.forEach(p => {
    const o = document.createElement("option");
    o.value = p.path;
    o.textContent = `${p.name} · ${p.site}`;
    sel.appendChild(o);
  });
  if (keep) sel.value = keep;
  updateMeta();
}
function updateMeta() {
  const v = $("profile").value;
  const opt = [...$("profile").options].find(o => o.value === v);
  $("profile_meta").textContent = opt && v ? "这个 profile 来自第 1 步的探站结果" : "";
}
$("profile").addEventListener("change", updateMeta);

async function refreshLedger() {
  const rows = await (await fetch("/api/ledger")).json();
  const body = $("ledger");
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5" class="muted">暂无记录</td></tr>'; return;
  }
  body.innerHTML = rows.map(r => {
    const cls = r.outcome === "success" ? "ok" : (r.outcome === "declined" ? "warn" : "bad");
    return `<tr><td class="muted">${esc((r.started_at||"").replace("T"," "))}</td>` +
      `<td>${esc(r.site||"")}</td>` +
      `<td><span class="pill ${cls}">${esc(r.outcome||"")}</span></td>` +
      `<td class="muted">${esc(r.stage||"")}</td>` +
      `<td class="muted">${esc(r.email||"—")}</td></tr>`;
  }).join("");
}

refreshProfiles(); refreshLedger(); drawSteps("", 0);
</script>
</body>
</html>
"""
