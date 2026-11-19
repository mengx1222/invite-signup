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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import ledger as ledger_mod
from .failures import Action, classify
from .human import click_first_enabled_non_google, human_click, human_type, launch_browser
from .ledger import scrub_url
from .mailbox import ImapProvider, Mailbox
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
    page_text: str = ""
    mail_dump: str = ""
    # How to reopen the temporary inbox later.  Empty when the mailbox was
    # supplied by the operator (they already own it) or the provider cannot
    # be reopened.
    inbox: Dict[str, Any] = field(default_factory=dict)
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
            "page_text": self.page_text,
            "mail_dump": self.mail_dump,
            "inbox": self.inbox,
            "errors": self.errors,
            "verdict_state": self.verdict_state,
            "notes": self.notes,
            "failure_kind": self.failure_kind,
            "failure_action": self.failure_action,
        }


def make_password(length: int = 16) -> str:
    """A password that satisfies the usual complexity rules."""
    import secrets
    import string

    # secrets, not random: this is a credential.  `random` is a Mersenne
    # Twister whose output is recoverable from a handful of observed values,
    # which is the wrong property for something a site will accept as a
    # password.
    lower = secrets.choice(string.ascii_lowercase)
    upper = secrets.choice(string.ascii_uppercase)
    digit = secrets.choice(string.digits)
    sym1 = secrets.choice("!@#$%&*")
    sym2 = secrets.choice("_-=+")
    pool = string.ascii_letters + string.digits + "!@#$%&*_-=+"
    chars = [lower, upper, digit, sym1, sym2] + [
        secrets.choice(pool) for _ in range(max(0, length - 5))
    ]
    # Fisher-Yates with secrets, so the guaranteed-character positions are
    # not predictable either.
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def run_registration(
    profile: SiteProfile,
    output_dir: str | Path = "./output",
    email: Optional[str] = None,
    password: Optional[str] = None,
    inbox_password: Optional[str] = None,
    allow_multi: bool = False,
    force: bool = False,
    headless: bool = True,
    tos_forbids_automation: bool = False,
    poll_timeout: int = 120,
    on_event: Optional[EventFn] = None,
    link_prompt: Optional[Callable[[], Optional[str]]] = None,
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

    try:
        result = _attempt(
            profile,
            out=out,
            key=key,
            book=book,
            email=email,
            password=password,
            inbox_password=inbox_password,
            allow_multi=allow_multi,
            force=force,
            headless=headless,
            tos_forbids_automation=tos_forbids_automation,
            poll_timeout=poll_timeout,
            on_event=on_event,
            link_prompt=link_prompt,
        )
    except Exception as exc:
        # An exception used to escape before the ledger row was written, so a
        # crashed run left no trace at all -- indistinguishable from never
        # having tried, which is precisely what the ledger exists to prevent.
        result = RunResult(stage="crashed", email=email or "")
        result.errors.append(f"{type(exc).__name__}: {exc}")
        _emit(on_event, "error", message=result.errors[-1])
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
    # A skip is not a failure: the duplicate guard refused to create a second
    # account, and nothing went wrong.  Leaving it unclassified made a batch
    # driver read "unknown / stop" and treat a healthy skip as an incident.
    if result.stage == "skipped-duplicate":
        result.failure_kind = ""
        result.failure_action = ""
        if not result.notes:
            result.notes = "already done today; nothing was attempted"
        return
    # "Already registered" is a business refusal: the account exists, so a
    # retry cannot succeed and must not create a second one.
    if result.stage == "already-registered":
        result.failure_kind = "site_rejected"
        result.failure_action = "abort"
        if not result.notes:
            result.notes = "the address already has an account on this site"
        return
    text = "; ".join(result.errors) or result.stage
    # A failure after the submit step means the site may already have created
    # the account, which is exactly what flips "retry" into "abort".
    mail_requested = result.stage not in (
        "start", "landing", "email", "password", "blocked-by-verdict",
        "skipped-duplicate", "blocked-by-quota", "mailbox-failed",
        "already-registered", "crashed",
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
    inbox_password: Optional[str],
    allow_multi: bool,
    force: bool,
    headless: bool,
    tos_forbids_automation: bool,
    poll_timeout: int,
    on_event: Optional[EventFn] = None,
    link_prompt: Optional[Callable[[], Optional[str]]] = None,
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
        if inbox_password:
            # The operator supplied their own inbox *and* its app password,
            # so the whole flow stays automatic: no waiting for a human to
            # paste the link.  Without the password the run still stops at
            # the verification step, which is the --wait-link path.
            # operator_owned=True makes Mailbox.export() return nothing, so
            # the app password for someone's real inbox cannot be persisted
            # even by accident.  The rule lives in Mailbox because that is
            # where export() is defined and where a future caller will look.
            mailbox = Mailbox(dump_path=dump_path, operator_owned=True)
            try:
                provider = ImapProvider()
                provider.configure(email, inbox_password)
                mailbox.provider = provider
                mailbox.address = provider.create()
                result.inbox = mailbox.export()
                mail_baseline = mailbox.current_ids()
            except Exception as exc:
                result.stage = "mailbox-failed"
                result.errors.append(
                    f"could not open your inbox: {type(exc).__name__}: {exc}"
                )
                _emit(on_event, "stage", stage=result.stage,
                      message="could not open your inbox")
                return result
    else:
        mailbox = Mailbox(dump_path=dump_path)
        try:
            # A site that keeps refusing one provider's domain should not cost
            # a failed run every time.  The preference is recorded in the
            # profile; with no preference this is just the default order.
            preferred = list(getattr(profile, "mailbox_prefer", []) or [])
            result.email = mailbox.create_preferring(preferred) if preferred else mailbox.create()
            result.inbox = mailbox.export()
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
                    # The URL is scrubbed here, not later: a verification
                    # link like /api/auth/verify-email?token=<JWT> is a live
                    # credential, and this trace is rendered into the
                    # diagnosis block the operator is told to copy and send
                    # to someone.  The ledger already scrubbed by value; the
                    # trace did not, so the one place built for sharing was
                    # the one place leaking.
                    result.api_trace.append(
                        f"{resp.request.method} {scrub_url(resp.url)} -> {resp.status}"
                    )

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

            # The site may refuse the mailbox right here, before any mail is
            # sent.  Catching it now saves a two-minute wait and, more
            # importantly, says *why* instead of "no verification link".
            # A site that already knows this address sends it to sign-in
            # instead of showing the rest of the signup form.  Reporting the
            # missing password field as a timeout classified the run as a
            # retryable transport error, which would have retried forever
            # against an account that already exists.
            if _landed_on_sign_in(page, profile):
                result.stage = "already-registered"
                result.errors.append(
                    "this address already has an account on this site; "
                    "the invite page redirected to sign-in"
                )
                result.notes = (
                    "use a different address, or sign in with this one instead "
                    "of registering again"
                )
                _emit(on_event, "stage", stage=result.stage,
                      message="this address is already registered")
                return result

            page_text = _visible_error_text(page)
            if page_text and _mailbox_was_rejected(page_text):
                result.stage = "mailbox-rejected"
                result.errors.append(f"the site refused this mailbox: {page_text[:200]}")
                result.notes = (
                    "supply a real inbox with --email <address>; "
                    "the run will then stop at the verification step so you can paste the link"
                )
                _emit(on_event, "stage", stage=result.stage, message=page_text[:160])
                return result

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
            # No password field is not an error: magic-link and OTP-only sites
            # send the mail straight after the email step.  Reporting "no
            # password selector" here made the runner reject a shape the
            # verdict had already declared automatable -- the two disagreed.

            result.stage = "submit"
            _emit(on_event, "stage", stage=result.stage, message="submitting the form")
            submit_selector = profile.selectors.get("submit")
            if submit_selector:
                if not human_click(page, submit_selector):
                    result.errors.append("submit click failed")
                    return result
            else:
                click_first_enabled_non_google(page)
            page.wait_for_timeout(2500)

            # --- verification ---
            mode = profile.verification.mode
            if mode == "link":
                result.stage = "await-verification-link"
                if link_prompt is not None:
                    # Half-automatic mode: the site sends the mail to a real
                    # inbox this tool cannot read, so a person supplies the
                    # link.  Everything before and after stays automated.
                    _emit(on_event, "stage", stage=result.stage,
                          message="waiting for you to paste the verification link")
                    link = link_prompt()
                    if not link:
                        result.errors.append("no verification link was supplied")
                        return result
                else:
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
                        on_tick=_wait_ticker(on_event, result.stage, poll_timeout),
                    )
                    if not poll.found:
                        result.errors.append("no verification link arrived: " + "; ".join(poll.errors[-3:]))
                        return result
                    link = poll.payload
                result.stage = "confirm-link"
                _emit(on_event, "stage", stage=result.stage, message="opening the verification link")
                try:
                    page.goto(link, wait_until="domcontentloaded", timeout=40000)
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
                    on_tick=_wait_ticker(on_event, result.stage, poll_timeout),
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
                result.page_text = _visible_error_text(page)
                result.errors.append("no session after the flow")
                if result.page_text:
                    result.errors.append(f"page says: {result.page_text[:200]}")
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
            try:
                result.page_text = _visible_error_text(page)
            except Exception:
                pass
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
                # Without this the temporary inbox is write-only: a password
                # reset from the site would land somewhere nobody can read.
                "inbox": result.inbox,
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
            # A skip is neither success nor failure: nothing went wrong, the
            # duplicate guard simply declined to create a second account.  It
            # gets its own outcome so the UI and the quota counter can tell
            # the three apart.
            "outcome": (
                "success" if result.ok
                else "skipped" if result.stage == "skipped-duplicate"
                else "failure"
            ),
            "stage": result.stage,
            "email": result.email,
            "verified": result.verified,
            "reward_credits": profile.reward_credits,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration_s": duration,
            "snapshot": result.snapshot,
            "page_text": result.page_text[:300],
            "mail_dump": result.mail_dump,
            "last_error": "; ".join(result.errors[-3:]) if result.errors else "",
            # _classify_outcome says the decision is "visible in the ledger",
            # and it was not: only prose was written, so a batch driver had to
            # re-derive the classification from text -- which drifts as the
            # rules evolve.
            "failure_kind": result.failure_kind,
            "failure_action": result.failure_action,
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
    saw_visible = False
    for index in range(count):
        element = candidates.nth(index)
        try:
            if not element.is_visible():
                continue
        except Exception:
            continue
        saw_visible = True
        try:
            src = element.get_attribute("src") or ""
        except Exception:
            src = ""
        if src:
            visible_srcs.append(src)

    if not saw_visible:
        return ""
    if not visible_srcs:
        # A visible container with no src is still a challenge widget, but it
        # says nothing about *which* one -- so it is classified only after
        # every element has been inspected.  Doing this inside the loop meant
        # the first src-less element masked every later one, and a real
        # Turnstile iframe behind it was reported as a generic "captcha".
        visible_srcs.append("<visible-widget>")
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


def _wait_ticker(on_event: Optional[EventFn], stage: str, total: int):
    """Turn a mailbox poll tick into a progress event.

    Emits at most once a second and only when the whole-second value changes,
    so a two-minute wait produces a steady trickle rather than a flood.
    """
    if on_event is None:
        return None
    state = {"last": -1}

    def tick(elapsed: int, remaining: int) -> None:
        if elapsed == state["last"]:
            return
        state["last"] = elapsed
        _emit(on_event, "waiting", stage=stage, elapsed=elapsed,
              remaining=remaining, total=total)

    return tick


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
    """Confirm the account is signed in, using whatever this site exposes.

    The session endpoint is site-specific -- `/api/auth/get-session` is a
    Better Auth convention, not a universal one.  Falling back to it when a
    profile has none made the pipeline quietly Yila-shaped: it would "work"
    everywhere the profile happened to come from a Better Auth site and fail
    everywhere else with "no session after the flow".

    So: use the profile's endpoint if it has one.  If it does not, fall back
    to the state endpoints (any of them returning 200 with a body proves a
    session exists), and only then report that the profile cannot confirm a
    login.
    """
    endpoint = profile.endpoints.get("session")
    if endpoint:
        payload = _in_page_json(page, endpoint)
        if isinstance(payload, dict):
            user = payload.get("user")
            if user:
                return user
            # Some sites return the user object at the top level.
            if payload.get("email") or payload.get("id"):
                return payload

    for _, state_path in (profile.endpoints_state or {}).items():
        payload = _in_page_json(page, state_path)
        if payload:
            return {"_state_only": True, "_endpoint": state_path}
    return None


# Phrases sites use when they refuse a mailbox.  Matching these early turns a
# 120-second wait-for-mail into an immediate, actionable failure.
# Re-exported for callers that imported it from here; the definition lives in
# failures.py so there is exactly one list to maintain.
from .failures import MAILBOX_REJECT_HINTS  # noqa: E402,F401


def _visible_error_text(page, limit: int = 400) -> str:
    """Collect whatever the page is currently telling the user.

    A screenshot shows a human what happened; this shows the *reason* without
    opening the image.  Both are kept, because "stuck at the password step"
    is far less useful than the red text next to the password field.
    """
    try:
        return page.evaluate(
            """(limit) => {
                const sel = '[role="alert"], [data-err], [data-emailmsg], .error, .text-red-500, ' +
                            '.text-destructive, [aria-invalid="true"] ~ *';
                const parts = [];
                document.querySelectorAll(sel).forEach(el => {
                    const t = (el.innerText || el.textContent || '').trim();
                    if (t) parts.push(t);
                });
                return parts.join(' | ').slice(0, limit);
            }""",
            limit,
        ) or ""
    except Exception:
        return ""


def _landed_on_sign_in(page, profile: SiteProfile) -> bool:
    """True when the invite page sent us to a sign-in page.

    That is the site's way of saying "this address already exists".  Detecting
    it matters because the alternative is a timeout on a password field that
    will never appear, which reads as a network problem and invites a retry.
    """
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    sign_in = (profile.sign_in_endpoint or "").lower()
    if sign_in and sign_in in url:
        return True
    if any(token in url for token in ("/sign-in", "/signin", "/login")):
        return True
    # Some sites keep the invite URL and swap the form instead.
    try:
        if page.locator('input[type="password"]').count() and not page.locator(
            profile.selectors.get("password", "#invite-password")
        ).count():
            return "sign in" in (page.locator("body").inner_text() or "").lower()
    except Exception:
        pass
    return False


def _mailbox_was_rejected(text: str) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in MAILBOX_REJECT_HINTS)
