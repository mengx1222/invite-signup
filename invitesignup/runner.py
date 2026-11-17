"""The executor: drive one invite-gated registration from a profile.

Guardrails enforced here, not merely documented:

* the verdict must allow the run -- a captcha or a terms problem stops it
* one invite code produces one account by default; running more requires an
  explicit flag *and* headroom in the site's own referral quota
* every step is recorded, so a failure says which stage died
* success is only reported after the server confirms a session
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import ledger as ledger_mod
from .failures import Action, classify
from .human import click_first_enabled_non_google, human_click, human_type, launch_browser
from .mailbox import Mailbox
from .profile import SiteProfile
from .verdict import decide

# A progress sink.  The CLI leaves it None and reads the final RunResult; a
# web front-end passes a callable and gets each stage change as it happens.
EventFn = Callable[[str, Dict[str, Any]], None]


def _emit(on_event: Optional[EventFn], kind: str, **payload: Any) -> None:
    """Report progress without letting a broken listener break the run."""
    if on_event is None:
        return
    try:
        on_event(kind, payload)
    except Exception:
        pass


@dataclass
class RunResult:
    ok: bool = False
    stage: str = "start"
    email: str = ""
    password: str = ""
    verified: bool = False
    credits: Any = None
    snapshot: str = ""
    mail_dump: str = ""
    errors: List[str] = field(default_factory=list)
    api_trace: List[str] = field(default_factory=list)
    verdict_state: str = ""
    notes: str = ""
    failure_kind: str = ""
    failure_action: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "stage": self.stage,
            "email": self.email,
            "verified": self.verified,
            "credits": self.credits,
            "snapshot": self.snapshot,
            "mail_dump": self.mail_dump,
            "errors": self.errors,
            "verdict_state": self.verdict_state,
            "notes": self.notes,
            "failure_kind": self.failure_kind,
            "failure_action": self.failure_action,
        }


def make_password(length: int = 16) -> str:
    """A password that satisfies the usual complexity rules."""
    import random
    import string

    lower = random.choice(string.ascii_lowercase)
    upper = random.choice(string.ascii_uppercase)
    digit = random.choice(string.digits)
    sym1 = random.choice("!@#$%&*")
    sym2 = random.choice("_-=+")
    pool = string.ascii_letters + string.digits + "!@#$%&*_-=+"
    chars = [lower, upper, digit, sym1, sym2] + [random.choice(pool) for _ in range(max(0, length - 5))]
    random.shuffle(chars)
    return "".join(chars)


def run_registration(
    profile: SiteProfile,
    output_dir: str | Path = "./output",
    email: Optional[str] = None,
    password: Optional[str] = None,
    allow_multi: bool = False,
    force: bool = False,
    headless: bool = True,
    tos_forbids_automation: bool = False,
    poll_timeout: int = 120,
    on_event: Optional[EventFn] = None,
) -> RunResult:
    """Execute one registration described by ``profile``.

    Thin wrapper whose only job is to guarantee a ledger row: whatever path
    the inner attempt takes -- blocked, skipped, failed, or successful -- one
    row is written before this returns.
    """
    started = time.time()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    key = ledger_mod.dedupe_key(profile.site, profile.invite_code)
    book = ledger_mod.Ledger.open(out / "ledger.jsonl")

    result = _attempt(
        profile,
        out=out,
        key=key,
        book=book,
        email=email,
        password=password,
        allow_multi=allow_multi,
        force=force,
        headless=headless,
        tos_forbids_automation=tos_forbids_automation,
        poll_timeout=poll_timeout,
        on_event=on_event,
    )
    _classify_outcome(result)
    _persist(book, profile, result, key, started, round(time.time() - started, 2))
    _emit(on_event, "finished", result=result.to_dict())
    return result


def _classify_outcome(result: RunResult) -> None:
    """Attach a retry verdict to a failed run.

    The classification is advisory for a single run -- this function never
    retries on its own -- but it is what a batch driver reads to decide
    whether to continue.  Recording it means the decision is visible in the
    ledger instead of being re-derived from an error string later.
    """
    if result.ok:
        result.failure_kind = ""
        result.failure_action = ""
        return
    text = "; ".join(result.errors) or result.stage
    # A failure after the submit step means the site may already have created
    # the account, which is exactly what flips "retry" into "abort".
    mail_requested = result.stage not in (
        "start", "landing", "email", "password", "blocked-by-verdict",
        "skipped-duplicate", "blocked-by-quota", "mailbox-failed",
    )
    classification = classify(text, mail_requested=mail_requested)
    result.failure_kind = classification.kind.value
    result.failure_action = classification.action.value
    if classification.action is Action.STOP and not result.notes:
        result.notes = classification.reason


def _attempt(
    profile: SiteProfile,
    *,
    out: Path,
    key: str,
    book,
    email: Optional[str],
    password: Optional[str],
    allow_multi: bool,
    force: bool,
    headless: bool,
    tos_forbids_automation: bool,
    poll_timeout: int,
    on_event: Optional[EventFn] = None,
) -> RunResult:
    """Do the work.  Never persists -- ``run_registration`` owns that."""
    from playwright.sync_api import sync_playwright

    started = time.time()
    result = RunResult()

    verdict = decide(profile, tos_forbids_automation=tos_forbids_automation)
    result.verdict_state = verdict.state
    if not verdict.allowed:
        result.stage = "blocked-by-verdict"
        result.errors.append(verdict.summary())
        result.notes = verdict.human_step
        _emit(on_event, "stage", stage=result.stage, message=verdict.summary())
        return result

    # quota + dedupe
    if not force and book.already_done(key):
        result.stage = "skipped-duplicate"
        result.errors.append(f"dedupe: {key} already completed")
        _emit(on_event, "stage", stage=result.stage, message=f"dedupe: {key}")
        return result
    if profile.max_successful_invites is not None:
        used = len([r for r in book.find(key) if r.get("outcome") == "success"])
        if used >= profile.max_successful_invites and not allow_multi:
            result.stage = "blocked-by-quota"
            result.errors.append(
                f"quota: {used}/{profile.max_successful_invites} invite slots used; "
                f"pass --allow-multi to continue"
            )
            _emit(on_event, "stage", stage=result.stage,
                  message=f"quota {used}/{profile.max_successful_invites}")
            return result

    # mailbox
    mailbox: Optional[Mailbox] = None
    mail_baseline: set = set()
    dump_path = out / f"mail_dump_{profile.site}.txt"
    if email:
        result.email = email
    else:
        mailbox = Mailbox(dump_path=dump_path)
        try:
            result.email = mailbox.create()
        except Exception as exc:
            result.stage = "mailbox-failed"
            result.errors.append(f"{type(exc).__name__}: {exc}")
            _emit(on_event, "stage", stage=result.stage, message=str(exc))
            return result
        # Capture the "already there" set *before* the site is asked to send
        # anything.  Without this, a leftover message from an earlier run can
        # be read as the verification for this one.
        mail_baseline = mailbox.current_ids()
    result.mail_dump = str(dump_path)
    result.password = password or make_password()
    _emit(on_event, "identity", email=result.email, has_password=bool(result.password))

    snapshot_dir = out / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = launch_browser(p, headless=headless)
        try:
            context = browser.new_context(no_viewport=True, locale=profile.locale)
            page = context.new_page()

            def on_response(resp):
                if "/api/" in resp.url:
                    result.api_trace.append(f"{resp.request.method} {resp.url} -> {resp.status}")

            page.on("response", on_response)

            # --- open the invite landing page ---
            result.stage = "landing"
            _emit(on_event, "stage", stage=result.stage, message="opening the invite page")
            page.goto(profile.invite_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)

            # The probe may only have *suspected* a captcha.  The live DOM is
            # the real gate: if a challenge is actually mounted, stop before
            # doing anything irreversible.
            challenge = _live_captcha(page)
            if challenge:
                result.stage = "blocked-by-captcha"
                result.errors.append(f"a {challenge} challenge is mounted in the live page; stopping")
                _emit(on_event, "stage", stage=result.stage, message=f"{challenge} challenge")
                return result

            # --- email step ---
            result.stage = "email"
            _emit(on_event, "stage", stage=result.stage, message=f"typing {result.email}")
            email_selector = profile.selectors.get("email")
            if not email_selector:
                result.errors.append("no email selector in the profile")
                return result
            human_type(page, email_selector, result.email)
            page.wait_for_timeout(400)
            if not click_first_enabled_non_google(page):
                result.errors.append("could not click the continue button")
                return result
            page.wait_for_timeout(2500)

            # --- password step ---
            result.stage = "password"
            _emit(on_event, "stage", stage=result.stage, message="setting the password")
            password_selector = profile.selectors.get("password")
            if password_selector:
                human_type(page, password_selector, result.password)
                confirm_selector = profile.selectors.get("confirm_password")
                if confirm_selector:
                    human_type(page, confirm_selector, result.password)
                page.wait_for_timeout(300)
                submit_selector = profile.selectors.get("submit")
                if submit_selector:
                    if not human_click(page, submit_selector):
                        result.errors.append("submit click failed")
                        return result
                else:
                    click_first_enabled_non_google(page)
                page.wait_for_timeout(2500)
            else:
                result.errors.append("no password selector in the profile")
                return result

            # --- verification ---
            mode = profile.verification.mode
            if mode == "link":
                result.stage = "await-verification-link"
                _emit(on_event, "stage", stage=result.stage,
                      message=f"waiting for mail (up to {poll_timeout}s)")
                if mailbox is None:
                    result.errors.append("verification needs a mailbox but none was available")
                    return result
                poll = mailbox.poll(
                    mode="link",
                    pattern=profile.verification.pattern,
                    link_hints=_link_hints(profile),
                    timeout_secs=poll_timeout,
                    not_before=started,
                    before_ids=mail_baseline,
                )
                if not poll.found:
                    result.errors.append("no verification link arrived: " + "; ".join(poll.errors[-3:]))
                    return result
                result.stage = "confirm-link"
                _emit(on_event, "stage", stage=result.stage, message="opening the verification link")
                try:
                    page.goto(poll.payload, wait_until="domcontentloaded", timeout=40000)
                except Exception as exc:
                    result.errors.append(f"verification link navigation: {type(exc).__name__}: {exc}")
                deadline = time.time() + 30
                while time.time() < deadline:
                    if _in_page_session(page, profile):
                        break
                    time.sleep(1.0)

            elif mode == "code":
                result.stage = "await-verification-code"
                _emit(on_event, "stage", stage=result.stage,
                      message=f"waiting for the code (up to {poll_timeout}s)")
                if mailbox is None:
                    result.errors.append("verification needs a mailbox but none was available")
                    return result
                poll = mailbox.poll(
                    mode="code",
                    pattern=profile.verification.pattern or r"\b\d{4,8}\b",
                    timeout_secs=poll_timeout,
                    not_before=started,
                    before_ids=mail_baseline,
                )
                if not poll.found:
                    result.errors.append("no verification code arrived: " + "; ".join(poll.errors[-3:]))
                    return result
                otp_selector = profile.selectors.get("otp")
                if otp_selector:
                    human_type(page, otp_selector, poll.payload)
                    submit_selector = profile.selectors.get("submit")
                    if submit_selector:
                        human_click(page, submit_selector)
                    page.wait_for_timeout(2500)

            # --- confirm ---
            result.stage = "confirm"
            _emit(on_event, "stage", stage=result.stage, message="checking the session")
            session_user = _in_page_session(page, profile)
            if not session_user:
                shot = snapshot_dir / f"{profile.site}_{int(started)}.png"
                try:
                    page.screenshot(path=str(shot))
                    result.snapshot = str(shot)
                except Exception:
                    pass
                result.errors.append("no session after the flow")
                return result

            result.verified = bool(session_user.get("emailVerified", True))
            result.stage = "state"
            _emit(on_event, "stage", stage=result.stage, message="reading account state")
            for label, path in (profile.endpoints_state or {}).items():
                result.credits = _in_page_json(page, path)
                break

            result.ok = True
        except Exception as exc:
            result.stage = result.stage or "exception"
            result.errors.append(f"{type(exc).__name__}: {exc}")
            _emit(on_event, "error", stage=result.stage, message=str(exc))
        finally:
            try:
                browser.close()
            except Exception:
                pass

    # The ledger row is written by run_registration(), which owns persistence.
    if result.ok:
        accounts = out / "accounts.jsonl"
        with open(accounts, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "site": profile.site,
                "email": result.email,
                "password": result.password,
                "invite_code": profile.invite_code,
                "verified": result.verified,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }, ensure_ascii=False) + "\n")

    return result


def _persist(book, profile: SiteProfile, result: RunResult, key: str,
             started: float, duration: float) -> None:
    """Write exactly one ledger row for this attempt.

    Every exit path must land here.  The first version returned early from
    about a dozen guard clauses, which meant a blocked or failed run left no
    trace at all -- the ledger silently under-counted attempts and the
    dedupe/quota logic had nothing to read.
    """
    try:
        book.append({
            "site": profile.site,
            "invite_code": profile.invite_code,
            "dedupe_key": key,
            "verdict": result.verdict_state,
            "outcome": "success" if result.ok else "failure",
            "stage": result.stage,
            "email": result.email,
            "verified": result.verified,
            "reward_credits": profile.reward_credits,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration_s": duration,
            "snapshot": result.snapshot,
            "mail_dump": result.mail_dump,
            "last_error": "; ".join(result.errors[-3:]) if result.errors else "",
            "notes": result.notes,
        })
    except Exception as exc:  # the ledger must never take the run down with it
        result.errors.append(f"ledger write failed: {type(exc).__name__}: {exc}")


CAPTCHA_SELECTOR = (
    'iframe[src*="challenges.cloudflare.com" i], '
    'iframe[src*="hcaptcha" i], '
    'iframe[src*="recaptcha" i], '
    'iframe[title*="captcha" i], '
    'iframe[title*="challenge" i], '
    '[class*="captcha" i], [id*="turnstile" i], [class*="turnstile" i]'
)


def _live_captcha(page) -> str:
    """Return the kind of captcha *currently visible*, or an empty string.

    Visibility matters.  Captcha vendors routinely leave a hidden container in
    the DOM (``<div id="cf-turnstile-...">`` with zero size, or a widget that
    only renders once the site decides to challenge you), and a selector that
    only checks for existence reports a challenge on pages that never show
    one.  That would contradict ``verdict``, which treats a bundle keyword as
    a mere suspicion -- the probe would say "automatable" and the run would
    then block itself.
    """
    try:
        candidates = page.locator(CAPTCHA_SELECTOR)
        count = candidates.count()
    except Exception:
        return ""
    if not count:
        return ""

    visible_srcs = []
    for index in range(count):
        element = candidates.nth(index)
        try:
            if not element.is_visible():
                continue
        except Exception:
            continue
        try:
            src = element.get_attribute("src") or ""
        except Exception:
            src = ""
        if src:
            visible_srcs.append(src)
        if not visible_srcs:
            # A visible container with no src is still a challenge widget.
            visible_srcs.append("<visible-widget>")

    if not visible_srcs:
        return ""
    blob = " ".join(visible_srcs).lower()
    if "challenges.cloudflare.com" in blob:
        return "turnstile"
    if "hcaptcha" in blob:
        return "hcaptcha"
    if "recaptcha" in blob:
        return "recaptcha"
    return "captcha"


def _link_hints(profile: SiteProfile) -> List[str]:
    hints = ["verify", "token", "callback", "confirm", "activate"]
    sample = profile.verification.sample or ""
    if sample:
        hints.append(sample.rsplit("/", 1)[-1][:24])
    return hints


def _in_page_json(page, path: str) -> Any:
    try:
        return page.evaluate(
            """async (p) => {
                const r = await fetch(p, {credentials: 'include'});
                if (!r.ok) return null;
                try { return await r.json(); } catch (e) { return null; }
            }""",
            path,
        )
    except Exception:
        return None


def _in_page_session(page, profile: SiteProfile) -> Optional[Dict[str, Any]]:
    endpoint = profile.endpoints.get("session")
    if not endpoint:
        endpoint = "/api/auth/get-session"
    payload = _in_page_json(page, endpoint)
    if isinstance(payload, dict):
        return payload.get("user") or None
    return None
