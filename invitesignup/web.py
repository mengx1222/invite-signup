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
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def add(self, kind: str, payload: Dict[str, Any]) -> None:
        entry = {"t": round(time.time() - self.started_at, 2), "kind": kind, **payload}
        self.events.append(entry)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "events": self.events,
            "result": self.result,
            "error": self.error,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
        }


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

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        job = store.get(job_id)
        if job is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(job.to_dict())

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
            return jsonify({"error": "invite_url is required"}), 400
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
                safe = "".join(c for c in name if c.isalnum() or c in "-_.")
                if safe and not safe.endswith(".json"):
                    safe += ".json"
                if safe:
                    saved = str(result.profile.save(Path.cwd() / "profiles" / safe))
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
            return jsonify({"error": "profile is required"}), 400
        try:
            profile = SiteProfile.load(path)
        except Exception as exc:
            return jsonify({"error": f"cannot read profile: {exc}"}), 400

        verdict = decide(profile, tos_forbids_automation=bool(body.get("tos_blocks", False)))
        if not verdict.allowed:
            return jsonify({
                "error": "the verdict does not allow a run",
                "verdict": verdict.to_dict(),
            }), 400
        if verdict.requires_confirmation and not body.get("yes"):
            return jsonify({
                "error": "this profile needs an explicit acknowledgement",
                "verdict": verdict.to_dict(),
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
                on_event=lambda kind, payload: job.add(kind, payload),
            )
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
            return jsonify({"error": "profile, email and password are required"}), 400
        profile = SiteProfile.load(path)

        def target(job: Job) -> Dict[str, Any]:
            job.add("stage", {"stage": "sign-in", "message": "signing in from a clean session"})
            report = verify_account(
                profile,
                email,
                password,
                sign_in_endpoint=profile.sign_in_endpoint,
                session_endpoint=profile.endpoints.get("session", "/api/auth/get-session"),
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
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  .wrap { max-width: 940px; margin: 0 auto; padding: 32px 20px 80px; }
  h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
  .sub { color: var(--dim); font-size: 13px; margin-bottom: 26px; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 18px 20px; margin-bottom: 16px;
  }
  .card h2 { font-size: 14px; margin: 0 0 14px; color: var(--dim); font-weight: 600;
             letter-spacing: 0.04em; text-transform: uppercase; }
  label { display: block; font-size: 13px; color: var(--dim); margin: 10px 0 4px; }
  input[type=text], input[type=password], select {
    width: 100%; padding: 9px 11px; border: 1px solid var(--line); border-radius: 7px;
    font: inherit; background: #fff; color: var(--ink);
  }
  input:focus, select:focus { outline: 2px solid #cfc9c0; outline-offset: -1px; }
  .row { display: flex; gap: 10px; }
  .row > * { flex: 1; }
  button {
    font: inherit; font-weight: 600; padding: 10px 18px; border-radius: 7px;
    border: 1px solid var(--accent); background: var(--accent); color: #fff; cursor: pointer;
  }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.ghost { background: #fff; color: var(--ink); border-color: var(--line); }
  .chk { display: flex; align-items: center; gap: 7px; margin-top: 10px; font-size: 13px; color: var(--dim); }
  .chk input { width: auto; }
  .log {
    font: 12.5px/1.7 ui-monospace, "Cascadia Mono", Consolas, monospace;
    background: #1c1b19; color: #e8e4de; border-radius: 8px; padding: 14px 16px;
    max-height: 420px; overflow: auto; white-space: pre-wrap; word-break: break-word;
  }
  .log .t { color: #8c857c; }
  .log .k-stage { color: #8ec6a8; }
  .log .k-error { color: #e08a80; }
  .log .k-verdict { color: #d9b36c; }
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
  .err { color: var(--bad); font-size: 13px; margin-top: 8px; }
  .hint { color: var(--dim); font-size: 12.5px; margin-top: 8px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>invite-signup</h1>
  <div class="sub">探站 → 建号 → 独立验收。本机运行，一次只跑一个任务。</div>

  <div class="card">
    <h2>1 · 探站</h2>
    <label>邀请链接</label>
    <input id="invite_url" type="text" placeholder="https://example.com/invite/CODE">
    <div class="row">
      <div>
        <label>保存为</label>
        <input id="save_as" type="text" placeholder="example（留空则不保存）">
      </div>
      <div>
        <label>语言</label>
        <select id="locale">
          <option value="en-US">en-US</option>
          <option value="zh-CN">zh-CN</option>
        </select>
      </div>
    </div>
    <div class="chk"><input id="headed_probe" type="checkbox"> 显示浏览器窗口</div>
    <div class="chk"><input id="no_advance" type="checkbox"> 不做存在性检查（看不到第二步字段）</div>
    <div class="chk"><input id="tos_blocks" type="checkbox"> 站点条款禁止自动化（会直接判定为不可跑）</div>
    <div style="margin-top:14px"><button id="btn_probe">开始探站</button></div>
    <div class="hint">只读。唯一的写操作是一次「邮箱是否已注册」的检查，不建号。</div>
    <div id="probe_err" class="err"></div>
  </div>

  <div class="card">
    <h2>2 · 建号</h2>
    <label>Profile</label>
    <select id="profile"><option value="">— 先探站或选择一个 —</option></select>
    <div class="row">
      <div>
        <label>邮箱（留空用一次性邮箱）</label>
        <input id="run_email" type="text" placeholder="you@outlook.com">
      </div>
      <div>
        <label>密码（留空自动生成）</label>
        <input id="run_password" type="password" placeholder="自动生成">
      </div>
    </div>
    <div class="row">
      <div>
        <label>等信超时（秒）</label>
        <input id="poll_timeout" type="text" value="120">
      </div>
    </div>
    <div class="chk"><input id="run_headed" type="checkbox"> 显示浏览器窗口</div>
    <div class="chk"><input id="run_yes" type="checkbox"> 我已读过 verdict 的附加条件（--yes）</div>
    <div class="chk"><input id="run_force" type="checkbox"> 忽略今天的去重记录</div>
    <div class="chk"><input id="run_multi" type="checkbox"> 允许超出邀请配额</div>
    <div style="margin-top:14px"><button id="btn_run">开始建号</button></div>
    <div class="hint">默认只建 1 个号。用满配额要单独勾选，这是保护不是限制。</div>
    <div id="run_err" class="err"></div>
  </div>

  <div class="card">
    <h2>3 · 独立验收</h2>
    <div class="row">
      <div>
        <label>邮箱</label>
        <input id="ver_email" type="text">
      </div>
      <div>
        <label>密码</label>
        <input id="ver_password" type="password">
      </div>
    </div>
    <div style="margin-top:14px"><button id="btn_verify" class="ghost">从干净会话验证</button></div>
    <div class="hint">执行者不能自己宣布成功。这一步重新登录，只看服务端说什么。</div>
    <div id="ver_err" class="err"></div>
  </div>

  <div class="card">
    <h2>进度</h2>
    <div id="status" class="muted" style="margin-bottom:10px">空闲</div>
    <div id="log" class="log">—</div>
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
let currentJob = null;

function logLine(entry) {
  const box = $("log");
  if (box.textContent.trim() === "—") box.textContent = "";
  const t = document.createElement("div");
  const stamp = entry.t !== undefined ? `[${entry.t.toFixed(1)}s] ` : "";
  const kind = entry.kind || "";
  t.innerHTML = `<span class="t">${stamp}</span><span class="k-${kind}">${kind}</span> ` +
    escapeHtml(describe(entry));
  box.appendChild(t);
  box.scrollTop = box.scrollHeight;
}

function describe(e) {
  if (e.kind === "stage") return `${e.stage}${e.message ? " — " + e.message : ""}`;
  if (e.kind === "verdict") {
    const v = e.verdict || {};
    return `[${v.state}] ${(v.reasons || []).join("; ")}`;
  }
  if (e.kind === "invite") {
    const p = e.payload || {};
    return `invite valid=${p.valid} reward=${p.rewardCredits} slots=${p.hasRewardSlots}`;
  }
  if (e.kind === "identity") return `identity ${e.email}`;
  if (e.kind === "error") return e.message || "error";
  if (e.kind === "finished") {
    const r = e.result || {};
    return `ok=${r.ok} verified=${r.verified} stage=${r.stage}`;
  }
  return e.message || JSON.stringify(e);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function setBusy(busy, label) {
  ["btn_probe", "btn_run", "btn_verify"].forEach(id => { $(id).disabled = busy; });
  $("status").textContent = busy ? (label || "运行中…") : "空闲";
}

function follow(jobId, label) {
  currentJob = jobId;
  $("log").textContent = "";
  setBusy(true, label);
  const es = new EventSource(`/api/jobs/${jobId}/stream`);
  es.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    if (data.kind === "_end") {
      es.close();
      setBusy(false);
      if (data.status === "failed") logLine({ kind: "error", message: data.error });
      if (data.result) onResult(data.result);
      refreshLedger();
      refreshProfiles();
      return;
    }
    logLine(data);
  };
  es.onerror = () => { es.close(); setBusy(false); };
}

function onResult(result) {
  if (result && result.verdict) {
    const v = result.verdict;
    const cls = v.allowed ? (v.state === "automatable" ? "ok" : "warn") : "bad";
    $("status").innerHTML = `<span class="pill ${cls}">${v.state}</span> ` +
      escapeHtml((v.reasons || []).join("; "));
    if (result.saved_as) {
      $("status").innerHTML += ` <span class="muted">→ ${escapeHtml(result.saved_as)}</span>`;
    }
  } else if (result && result.ok !== undefined) {
    const cls = result.ok ? "ok" : "bad";
    $("status").innerHTML = `<span class="pill ${cls}">ok=${result.ok} verified=${result.verified}</span>`;
    if (result.email) { $("ver_email").value = result.email; }
  } else if (result && result.verified !== undefined) {
    const cls = result.verified ? "ok" : "bad";
    $("status").innerHTML = `<span class="pill ${cls}">verified=${result.verified}</span>`;
  }
}

async function post(url, body, errId) {
  $(errId).textContent = "";
  const r = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    let msg = data.error || `HTTP ${r.status}`;
    if (data.verdict) {
      const v = data.verdict;
      msg += "\n" + (v.reasons || []).join("\n");
      if (v.human_step) msg += "\n→ " + v.human_step;
    }
    $(errId).textContent = msg;
    return null;
  }
  return data;
}

$("btn_probe").onclick = async () => {
  const data = await post("/api/probe", {
    invite_url: $("invite_url").value,
    save_as: $("save_as").value,
    locale: $("locale").value,
    headed: $("headed_probe").checked,
    advance: !$("no_advance").checked,
    tos_blocks: $("tos_blocks").checked,
  }, "probe_err");
  if (data) follow(data.job_id, "探站中…");
};

$("btn_run").onclick = async () => {
  const data = await post("/api/run", {
    profile: $("profile").value,
    email: $("run_email").value,
    password: $("run_password").value,
    poll_timeout: parseInt($("poll_timeout").value || "120", 10),
    headed: $("run_headed").checked,
    yes: $("run_yes").checked,
    force: $("run_force").checked,
    allow_multi: $("run_multi").checked,
  }, "run_err");
  if (data) follow(data.job_id, "建号中…");
};

$("btn_verify").onclick = async () => {
  const data = await post("/api/verify", {
    profile: $("profile").value,
    email: $("ver_email").value,
    password: $("ver_password").value,
  }, "ver_err");
  if (data) follow(data.job_id, "验收中…");
};

async function refreshProfiles() {
  const rows = await (await fetch("/api/profiles")).json();
  const sel = $("profile");
  const keep = sel.value;
  sel.innerHTML = '<option value="">— 选择一个 profile —</option>';
  rows.forEach(p => {
    const o = document.createElement("option");
    o.value = p.path;
    o.textContent = `${p.name} · ${p.site} · ${p.captcha}/${p.verification}`;
    sel.appendChild(o);
  });
  if (keep) sel.value = keep;
}

async function refreshLedger() {
  const rows = await (await fetch("/api/ledger")).json();
  const body = $("ledger");
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5" class="muted">暂无记录</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => {
    const cls = r.outcome === "success" ? "ok" : (r.outcome === "declined" ? "warn" : "bad");
    return `<tr>
      <td class="muted">${escapeHtml((r.started_at || "").replace("T", " "))}</td>
      <td>${escapeHtml(r.site || "")}</td>
      <td><span class="pill ${cls}">${escapeHtml(r.outcome || "")}</span></td>
      <td class="muted">${escapeHtml(r.stage || "")}</td>
      <td class="muted">${escapeHtml(r.email || "—")}</td>
    </tr>`;
  }).join("");
}

refreshProfiles();
refreshLedger();
</script>
</body>
</html>
"""
