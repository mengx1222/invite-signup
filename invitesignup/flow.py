"""One-click flow: an invite URL goes in, a verified account comes out.

Probe, run and verify exist as separate commands because each is useful on
its own.  This module chains them so a caller can hand over a single URL and
watch one progress stream.  The person using the web UI should never have to
know that "probe" and "verdict" are separate ideas -- those are our words,
not theirs.

What this does *not* do: relax any rule.  A verdict that forbids a run still
stops the flow, a captcha still stops it, and success still requires the
independent verification to agree.  Chaining phases changes the interface,
not the guarantees.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import ledger as ledger_mod
from .profile import SiteProfile
from .probe import probe
from .runner import run_registration
from .verdict import decide
from .verify import verify_account

EventFn = Callable[[str, Dict[str, Any]], None]

# Probe stage -> the step a user sees.  Probe's internal names ("chunks",
# "config") mean nothing outside this codebase.
_PROBE_STEP = {
    "landing": "open",
    "chunks": "understand",
    "config": "understand",
    "dom": "understand",
    "advance": "understand",
    "verdict": "understand",
}

_RUN_STEP = {
    # `landing` is deliberately absent: the probe already opened the page, so
    # mapping the run's landing onto "open" made the bar go backwards from
    # "understand" to "open".
    "email": "submit",
    "password": "submit",
    "submit": "submit",
    "await-verification-link": "mail",
    "await-verification-code": "mail",
    "confirm-link": "mail",
    "confirm": "verify",
    "state": "save",
}


def _translator(on_event: Optional[EventFn]):
    """Map internal stages onto the coarse steps the user actually sees."""
    if on_event is None:
        return None, None

    def probe_sink(kind: str, payload: Dict[str, Any]) -> None:
        stage = payload.get("stage") or ""
        step = _PROBE_STEP.get(stage)
        if kind == "substage":
            on_event("substage", {"stage": _PROBE_STEP.get(stage, "understand"),
                                  "done": payload.get("done"),
                                  "total": payload.get("total")})
            return
        if kind == "stage" and step:
            on_event("stage", {"stage": step, "message": payload.get("message", "")})
        elif kind in ("invite", "verdict"):
            on_event(kind, payload)

    def run_sink(kind: str, payload: Dict[str, Any]) -> None:
        stage = payload.get("stage") or ""
        step = _RUN_STEP.get(stage)
        if kind == "stage" and step:
            on_event("stage", {"stage": step, "message": payload.get("message", "")})
        elif kind == "waiting" and step:
            on_event("waiting", {"stage": step,
                                 "elapsed": payload.get("elapsed"),
                                 "remaining": payload.get("remaining"),
                                 "total": payload.get("total")})
        elif kind in ("identity", "error"):
            on_event(kind, payload)

    return probe_sink, run_sink


def run_flow(
    invite_url: str,
    *,
    output_dir: str | Path = "./output",
    profile_dir: str | Path = "profiles",
    headless: bool = True,
    poll_timeout: int = 120,
    on_event: Optional[EventFn] = None,
    link_prompt: Optional[Callable[[], Optional[str]]] = None,
) -> Dict[str, Any]:
    """Probe, decide, create one account, then verify it independently.

    Returns a dict shaped for the UI:

        {"ok": bool, "stage": str, "email": str, "password": str,
         "credits": Any, "reason": str, "failure_kind": str,
         "profile": {...}, "verdict": {...}, "trace": [...]}

    ``ok`` is True only when the *independent* verification agreed.  A
    successful registration that could not be verified is not success.
    """
    started = time.time()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(profile_dir)
    trace: list = []

    def emit(kind: str, **payload: Any) -> None:
        if on_event is None:
            return
        try:
            on_event(kind, payload)
        except Exception:
            pass

    probe_sink, run_sink = _translator(on_event)

    # ---- phase 1: understand the site ---------------------------------
    emit("stage", stage="open", message="打开邀请链接")
    try:
        found = probe(
            invite_url,
            headless=headless,
            on_event=probe_sink,
        )
    except Exception as exc:
        return {
            "ok": False, "stage": "understand", "email": "", "password": "",
            "credits": None, "failure_kind": "transport",
            "reason": f"打开邀请链接失败：{type(exc).__name__}: {exc}",
            "trace": trace, "elapsed": round(time.time() - started, 1),
        }

    profile = found.profile
    verdict = found.verdict or decide(profile)
    trace.append(f"probe: verdict={verdict.state} captcha={profile.captcha}")

    if not verdict.allowed:
        return {
            "ok": False, "stage": "understand", "email": "", "password": "",
            "credits": None, "failure_kind": _kind_for(verdict),
            "reason": _plain_reason(verdict),
            "verdict": verdict.to_dict(), "profile": profile.to_dict(),
            "trace": trace, "elapsed": round(time.time() - started, 1),
        }

    # Save the profile so a re-run of the same site does not re-probe.
    try:
        safe = "".join(c for c in profile.site if c.isalnum() or c in "-_.")
        if safe:
            profile.save(profile_dir / f"{safe}.auto.json")
    except Exception:
        pass

    # ---- phase 2: create the account ----------------------------------
    emit("stage", stage="submit", message="填写并提交注册")
    try:
        result = run_registration(
            profile,
            output_dir=out,
            headless=headless,
            poll_timeout=poll_timeout,
            on_event=run_sink,
            link_prompt=link_prompt,
        )
    except Exception as exc:
        return {
            "ok": False, "stage": "submit", "email": "", "password": "",
            "credits": None, "failure_kind": "unknown",
            "reason": f"注册过程出错：{type(exc).__name__}: {exc}",
            "profile": profile.to_dict(), "verdict": verdict.to_dict(),
            "trace": trace, "elapsed": round(time.time() - started, 1),
        }

    trace.extend(result.api_trace[-40:])

    if not result.ok:
        skipped = result.stage == "skipped-duplicate"
        return {
            "ok": False,
            "skipped": skipped,
            "stage": result.stage,
            "email": result.email,
            "password": result.password,
            "credits": None,
            "failure_kind": "" if skipped else (result.failure_kind or "unknown"),
            "reason": _plain_failure(result),
            "detail": result.errors[-4:],
            "page_text": result.page_text,
            "snapshot": result.snapshot,
            "profile": profile.to_dict(),
            "verdict": verdict.to_dict(),
            "trace": trace,
            "elapsed": round(time.time() - started, 1),
        }

    # ---- phase 3: prove it independently ------------------------------
    emit("stage", stage="verify", message="用干净会话验证账号")
    try:
        report = verify_account(
            profile,
            result.email,
            result.password,
            sign_in_endpoint=profile.sign_in_endpoint,
            session_endpoint=profile.endpoints.get("session", ""),
            state_endpoints=profile.endpoints_state,
        )
    except Exception as exc:
        return {
            "ok": False, "stage": "verify", "email": result.email,
            "password": result.password, "credits": None,
            "failure_kind": "verification",
            "reason": f"账号建好了，但验证时出错：{type(exc).__name__}: {exc}",
            "profile": profile.to_dict(), "verdict": verdict.to_dict(),
            "trace": trace, "elapsed": round(time.time() - started, 1),
        }

    emit("stage", stage="save", message="保存账号信息")

    payload = {
        "ok": bool(report.verified),
        "stage": "done" if report.verified else "verify",
        "email": result.email,
        "password": result.password,
        "credits": _balance(report.state),
        "failure_kind": "" if report.verified else "verification",
        "reason": "" if report.verified else (
            report.reason or "账号建好了，但没能独立验证；可能已可用，建议手动登录确认"
        ),
        "checks": report.checks,
        "state": report.state,
        "profile": profile.to_dict(),
        "verdict": verdict.to_dict(),
        "trace": trace,
        "elapsed": round(time.time() - started, 1),
    }
    return payload


def _balance(state: Dict[str, Any]) -> Any:
    """Pull a credit balance out of whatever the site calls it."""
    if not isinstance(state, dict):
        return None
    for _, payload in state.items():
        data = payload.get("data") if isinstance(payload, dict) else None
        for source in (data, payload):
            if isinstance(source, dict):
                for key in ("balance", "available", "credits", "quota"):
                    if key in source and isinstance(source[key], (int, float)):
                        return source[key]
    return None


def _kind_for(verdict) -> str:
    """Kept as a module-level name for callers, but the rule lives on Verdict."""
    return verdict.failure_kind()


def _plain_reason(verdict) -> str:
    """Say what stopped us in words a non-engineer can act on."""
    joined = " ".join(getattr(verdict, "blockers", []) or []).lower()
    if "no_signup_form" in joined or "wallet" in joined:
        return ("这个页面没有邮箱/密码输入框，它不是用表单注册的"
                "（通常是钱包签名或只能第三方登录）。这个工具只会填表单，"
                "也不会替你保管钱包私钥，所以这一类需要你手动注册一次。")
    if "captcha" in joined:
        return ("这个站点有验证码，本工具不会绕过。"
                "请先在浏览器里手动注册一次；如果验证码其实没出现，把诊断信息发我。")
    if "tos" in joined:
        return "这个站点的条款禁止自动化注册，已停止。"
    if "invite_absent" in joined:
        return "这个链接里没找到邀请码，可能不是邀请注册页。请确认链接是否完整。"
    if "invite_invalid" in joined:
        return "邀请码无效或已过期，请换一个新链接。"
    if "missing_selectors" in joined:
        return "没能识别出注册表单的输入框，把诊断信息发我，我来补规则。"
    return "；".join(getattr(verdict, "reasons", []) or ["无法继续"]) 


def _plain_failure(result) -> str:
    stage = result.stage
    if stage == "mailbox-rejected":
        return ("站点拒绝了一次性邮箱。把诊断信息发我，我给它配一个可用邮箱；"
                "或者你先用自己的邮箱注册一次，我再按那个流程调。")
    if stage in ("await-verification-link", "await-verification-code"):
        return "注册提交了，但没等到验证邮件。把诊断信息发我，我看看是邮件没到还是没解析出来。"
    if stage == "blocked-by-quota":
        return "这个邀请码的名额已经用完了。"
    if stage == "skipped-duplicate":
        return "今天已经用这个链接成功注册过，为防重复建号已跳过。"
    if stage == "blocked-by-captcha":
        return "注册过程中出现了验证码，已停止（本工具不绕过）。"
    if result.errors:
        return result.errors[0]
    return "注册没能完成。"
