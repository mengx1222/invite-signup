"""The prober: discover how an invite-gated site registers, without guessing.

The prober is the reusable part of this project.  Almost everything it does
is read-only:

1. fetch the invite landing page and pull the invite payload out of it
2. download every JS chunk the page loads and mine it for API endpoints
3. probe public config endpoints for feature flags
4. open the page in a real browser and read the form's DOM
5. optionally "advance" one step (an existence check -- it creates no account)
   to reveal fields that only exist after the first step
6. infer whether verification is link-based or code-based
7. emit a SiteProfile plus a verdict

Everything it cannot determine is left as ``unknown`` so the verdict can stop
the run instead of guessing.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import requests

from .human import launch_browser
from .profile import Attribution, SiteProfile, Verification
from .verdict import Verdict, decide

EventFn = Callable[[str, Dict[str, Any]], None]


def _emit(on_event: Optional[EventFn], kind: str, **payload: Any) -> None:
    if on_event is None:
        return
    try:
        on_event(kind, payload)
    except Exception:
        pass

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

CONFIG_PATHS = (
    "/api/config/public",
    "/api/config",
    "/api/public/config",
    "/api/settings/public",
)

CAPTCHA_KEYWORDS = {
    "turnstile": ("turnstile", "cf-turnstile", "challenges.cloudflare.com"),
    "recaptcha": ("recaptcha", "grecaptcha"),
    "hcaptcha": ("hcaptcha",),
    "akamai": ("_abck", "bmak"),
    "slider": ("slideverify", "geetest", "slidercaptcha", "puzzle-captcha"),
}

ENDPOINT_FRAGMENT_NEEDLES = (
    "/sign-up/email", "/sign-in/email", "/send-verification-email",
    "/verify-email", "/get-session", "/check-email", "/referral/check-email",
)

ENDPOINT_ROLES = {
    "check-email": ("check-email", "check_email", "email-exists", "exists"),
    "sign-up": ("sign-up/email", "signup/email", "sign_up/email", "register"),
    "send-verification": ("send-verification-email", "send-verification"),
    "verify": ("verify-email", "confirm-email"),
    "session": ("get-session", "session"),
    "credits": ("credits", "balance", "quota"),
    "referral": ("referral", "invite"),
}

PROBE_EMAIL_TEMPLATE = "invite-signup-probe-{rand}@example.com"


@dataclass
class ProbeResult:
    profile: SiteProfile
    findings: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    verdict: Optional[Verdict] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "findings": self.findings,
            "warnings": self.warnings,
            "verdict": self.verdict.to_dict() if self.verdict else None,
        }


# --------------------------------------------------------------------------
# step 1 -- the invite landing page
# --------------------------------------------------------------------------

def _iter_json_objects(text: str):
    """Yield every balanced ``{...}`` block in a blob of text."""
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start:i + 1]
                start = -1


def extract_invite_payload(html: str) -> Dict[str, Any]:
    """Find the invite payload in a Next.js / RSC rendered page."""
    unescaped = html.replace('\\"', '"').replace("\\u0026", "&")
    for blob in _iter_json_objects(unescaped):
        if "referralCode" not in blob and "inviteCode" not in blob and "rewardCredits" not in blob:
            continue
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        # The payload is usually nested one level deep, e.g.
        # {"landingData": {"referralCode": ...}} in a Next.js RSC payload.
        for candidate in (obj, *obj.values()):
            if not isinstance(candidate, dict):
                continue
            if "referralCode" in candidate or "inviteCode" in candidate:
                return candidate
    return {}


def extract_chunks(html: str, base_url: str) -> List[str]:
    """Return absolute URLs of every JS chunk the page loads."""
    srcs = re.findall(r'src="(/_next/static/chunks/[^"]+\.js)"', html)
    if not srcs:
        srcs = re.findall(r'src="([^"]+\.js)"', html)
    return [urllib.parse.urljoin(base_url, s) for s in dict.fromkeys(srcs)]


def scan_chunks(chunks: Sequence[str], timeout: int = 30) -> Dict[str, Any]:
    """Mine chunk source for API endpoints and captcha fingerprints."""
    api_paths: set = set()
    fragments: set = set()
    cookie_candidates: set = set()
    captcha_hits: Dict[str, List[str]] = {}
    verify_samples: List[str] = []
    fetched = 0

    for url in chunks:
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
            if r.status_code != 200:
                continue
            js = r.text
        except Exception:
            continue
        fetched += 1
        for path in re.findall(r'["\'](/api/[A-Za-z0-9_\-/\.]+)["\']', js):
            api_paths.add(path)
        # Many frontends build their endpoint at runtime: the bundle only
        # holds the fragment ("/verify-email") and a base path ("/api/auth").
        for frag in re.findall(r'["\'](/[a-z0-9\-]+(?:/[a-z0-9\-]+){0,3})["\']', js):
            if any(frag.endswith(needle) for needle in ENDPOINT_FRAGMENT_NEEDLES):
                fragments.add(frag)
        for path in re.findall(r'["\'](/api/[A-Za-z0-9_\-/\.]*(?:[Vv]erif|[Cc]onfirm)[A-Za-z0-9_\-/\.]*)["\']', js):
            verify_samples.append(path)
        # Invite attribution is often written by the client, not the server:
        #   document.cookie = "yila_invite_code=..."
        # so the cookie name has to be mined out of the bundles too.
        for name in re.findall(r'["\']([A-Za-z0-9_\-]*(?:invite|referral)[A-Za-z0-9_\-]*)["\']', js):
            cookie_candidates.add(name)
        lowered = js.lower()
        for kind, needles in CAPTCHA_KEYWORDS.items():
            for needle in needles:
                if needle.lower() in lowered:
                    captcha_hits.setdefault(kind, [])
                    if url not in captcha_hits[kind]:
                        captcha_hits[kind].append(url)

    return {
        "chunks_total": len(chunks),
        "chunks_fetched": fetched,
        "api_paths": sorted(api_paths),
        "endpoint_fragments": sorted(fragments),
        "invite_cookie_candidates": sorted(cookie_candidates),
        "captcha_hits": {k: v[:3] for k, v in captcha_hits.items()},
        "verify_path_samples": sorted(set(verify_samples))[:10],
    }


def classify_captcha(scan: Dict[str, Any], html: str, dom: Optional["DomInfo"] = None) -> str:
    """Decide whether a captcha is in play.

    A keyword in a JavaScript bundle is **not** evidence.  Bundled libraries,
    support widgets and even captcha *detection* code routinely mention
    "hcaptcha" or "recaptcha" without the site ever showing a challenge -- a
    real site was classified as hcaptcha-protected on exactly that basis.

    The ladder is therefore:

    * a captcha iframe / script / widget element in the live DOM  -> that kind
    * a captcha keyword in the bundles only                        -> "suspected"
    * nothing                                                     -> "none"

    "suspected" does not block the run, but it demands a human look, and the
    runner re-checks the live DOM before it does anything irreversible.
    """
    dom = dom or DomInfo()
    dom_sources = " ".join(list(dom.frames) + list(dom.scripts)).lower()
    if dom.captcha_nodes:
        dom_sources += " captcha challenge"
    for kind, needles in CAPTCHA_KEYWORDS.items():
        for needle in needles:
            if needle.lower() in dom_sources:
                return kind
    if dom.captcha_nodes:
        return "captcha"

    hits = dict(scan.get("captcha_hits") or {})
    lowered = html.lower()
    if "grecaptcha" in lowered or "hcaptcha" in lowered:
        hits.setdefault("recaptcha", [])
    if "cf-turnstile" in lowered:
        hits.setdefault("turnstile", [])
    if hits:
        return "suspected"
    return "none"


def role_endpoints(api_paths: Sequence[str], fragments: Sequence[str] = ()) -> Dict[str, str]:
    """Map raw API paths to canonical roles.

    Endpoint paths are assembled in two ways and both have to be handled:

    * a full path in the bundle ("/api/referral/check-email")
    * a base path plus a fragment ("/api/auth" + "/verify-email"), which is
      how most modern auth clients build their routes -- and how the
      verification endpoint stayed invisible to the first version of the
      scanner.
    """
    candidates = list(api_paths)
    base = ""
    for path in api_paths:
        if path.count("/") == 2 and path.startswith("/api/"):
            base = path
            break
    for frag in fragments:
        if base and not frag.startswith(base):
            candidates.append(base + frag)
        else:
            candidates.append(frag)

    out: Dict[str, str] = {}
    for role, needles in ENDPOINT_ROLES.items():
        for path in candidates:
            if any(n.lower() in path.lower() for n in needles):
                out.setdefault(role, path)
                break
    return out


# Paths that report live account state rather than performing an action.
# Finding these automatically matters: without them a successful run reports
# no balance, and "the account exists" is all you can say about it.
STATE_ENDPOINT_HINTS = {
    "credits": ("/api/credits", "/api/balance", "/api/quota", "/api/usage"),
    "referral": ("/api/referral/me", "/api/referral", "/api/invite/me"),
}


def find_state_endpoints(api_paths: Sequence[str]) -> Dict[str, str]:
    """Pick out the endpoints that return account state.

    These are matched exactly (or as a prefix) rather than by substring,
    because `/api/referral/check-email` also contains "referral" but is an
    action, not a state read.
    """
    out: Dict[str, str] = {}
    for label, candidates in STATE_ENDPOINT_HINTS.items():
        for candidate in candidates:
            for path in api_paths:
                if path == candidate or path.rstrip("/") == candidate.rstrip("/"):
                    out[label] = path
                    break
            if label in out:
                break
    return out


def find_sign_in_endpoint(api_paths: Sequence[str], fragments: Sequence[str]) -> str:
    """Locate the sign-in route, which is built the same way as sign-up."""
    for path in api_paths:
        if "sign-in" in path.lower() or "signin" in path.lower() or "login" in path.lower():
            return path
    base = ""
    for path in api_paths:
        if path.count("/") == 2 and path.startswith("/api/"):
            base = path
            break
    for fragment in fragments:
        if "sign-in" in fragment.lower() or "login" in fragment.lower():
            return (base + fragment) if base else fragment
    return ""


# --------------------------------------------------------------------------
# step 3 -- public config
# --------------------------------------------------------------------------

def probe_config(base_url: str, timeout: int = 20) -> Dict[str, Any]:
    """Look for a public config endpoint and return its flags."""
    out: Dict[str, Any] = {"found": False, "data": {}}
    for path in CONFIG_PATHS:
        try:
            r = requests.get(base_url + path, timeout=timeout, headers={"User-Agent": USER_AGENT})
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            payload = r.json()
        except Exception:
            continue
        data = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
        if isinstance(data, dict):
            out["found"] = True
            out["data"] = data
            out["path"] = path
            return out
    return out


# --------------------------------------------------------------------------
# step 4/5 -- browser probe
# --------------------------------------------------------------------------

DOM_SCRIPT = """() => {
    const pick = (el) => ({
        type: el.getAttribute('type') || '',
        id: el.id || '',
        name: el.getAttribute('name') || '',
        placeholder: el.getAttribute('placeholder') || '',
        testid: el.getAttribute('data-testid') || '',
        autocomplete: el.getAttribute('autocomplete') || '',
        inputmode: el.getAttribute('inputmode') || '',
        maxlength: el.getAttribute('maxlength') || '',
    });
    const inputs = [...document.querySelectorAll('input')].map(pick);
    const buttons = [...document.querySelectorAll('button')].map(b => ({
        text: (b.innerText || '').trim().slice(0, 40),
        type: b.getAttribute('type') || '',
        id: b.id || '',
        disabled: !!b.disabled,
    }));
    const labels = [...document.querySelectorAll('label')].map(l => ({
        for: l.htmlFor || '', text: (l.innerText || '').trim().slice(0, 40),
    }));
    const frames = [...document.querySelectorAll('iframe')].map(f => f.getAttribute('src') || '');
    const scripts = [...document.querySelectorAll('script[src]')].map(s => s.getAttribute('src') || '');
    // Captcha widgets are usually mounted as a container element with a
    // telling id/class/name, even before the iframe appears.
    const captchaNodes = [...document.querySelectorAll('[id*="captcha" i],[class*="captcha" i],[name*="captcha" i],[id*="turnstile" i],[class*="turnstile" i],[id*="challenge" i],[class*="challenge" i]')]
        .length;
    return {inputs, buttons, labels, frames, scripts, captchaNodes};
}"""


@dataclass
class DomInfo:
    inputs: List[Dict[str, Any]] = field(default_factory=list)
    buttons: List[Dict[str, Any]] = field(default_factory=list)
    labels: List[Dict[str, Any]] = field(default_factory=list)
    frames: List[str] = field(default_factory=list)
    scripts: List[str] = field(default_factory=list)
    captcha_nodes: int = 0
    url: str = ""
    title: str = ""


@dataclass
class TrafficLog:
    """What the page actually requested, as opposed to what the bundles mention.

    Bundle scanning finds endpoints that are *referenced*; request capture
    finds endpoints that are *called*.  Neither is a superset of the other:
    a route built at runtime (`base + "/verify-email"`) may never appear as a
    whole string in any bundle, while a route that only loads after sign-in
    never gets called during a probe.  Recording both and merging them is the
    only way to see the union.
    """

    paths: List[str] = field(default_factory=list)
    methods: Dict[str, List[str]] = field(default_factory=dict)

    def add(self, method: str, url: str, base_url: str = "") -> None:
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return
        path = parsed.path
        if not path.startswith("/api/"):
            return
        if path not in self.paths:
            self.paths.append(path)
        verbs = self.methods.setdefault(path, [])
        if method.upper() not in verbs:
            verbs.append(method.upper())

    def to_dict(self) -> Dict[str, Any]:
        return {"paths": sorted(self.paths),
                "methods": {k: sorted(v) for k, v in self.methods.items()}}


def _read_dom(page) -> DomInfo:
    data = page.evaluate(DOM_SCRIPT)
    return DomInfo(
        inputs=data.get("inputs") or [],
        buttons=data.get("buttons") or [],
        labels=data.get("labels") or [],
        frames=data.get("frames") or [],
        scripts=data.get("scripts") or [],
        captcha_nodes=int(data.get("captchaNodes") or 0),
        url=page.url,
        title=page.title(),
    )


def dom_probe(url: str, headless: bool = True, locale: str = "en-US") -> DomInfo:
    """Open the page in a real browser and read its form structure."""
    from playwright.sync_api import sync_playwright  # lazy: keeps the package importable without Playwright

    with sync_playwright() as p:
        browser = launch_browser(p, headless=headless)
        try:
            context = browser.new_context(no_viewport=True, locale=locale)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            return _read_dom(page)
        finally:
            browser.close()


def advance_probe(url: str, headless: bool = True, locale: str = "en-US",
                  probe_email: Optional[str] = None) -> DomInfo:
    """Fill the email step and press Continue, then read the form again.

    This performs exactly one *existence check* request.  It does not create
    an account.  It is the only way to see fields that only render after the
    first step (password fields, in practice).
    """
    from playwright.sync_api import sync_playwright

    email = probe_email or PROBE_EMAIL_TEMPLATE.format(rand=str(int(time.time()))[-6:])
    with sync_playwright() as p:
        browser = launch_browser(p, headless=headless)
        try:
            context = browser.new_context(no_viewport=True, locale=locale)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)

            mail = page.locator('input[type="email"]').first
            if mail.count() == 0:
                return _read_dom(page)

            mail.click()
            mail.type(email, delay=35)
            page.wait_for_timeout(400)

            clicked = False
            for selector in ('button[type="submit"]', 'button:not([disabled])'):
                buttons = page.locator(selector)
                for i in range(buttons.count()):
                    b = buttons.nth(i)
                    try:
                        text = (b.inner_text() or "").strip()
                    except Exception:
                        continue
                    if not text or "google" in text.lower():
                        continue
                    b.click()
                    clicked = True
                    break
                if clicked:
                    break
            page.wait_for_timeout(2500)
            return _read_dom(page)
        finally:
            browser.close()


def traffic_probe(url: str, headless: bool = True, locale: str = "en-US",
                  advance: bool = True,
                  probe_email: Optional[str] = None) -> TrafficLog:
    """Open the page and record every ``/api/`` request it actually makes.

    This is the complement to :func:`scan_chunks`.  Bundle scanning is
    generous (it sees routes that are only referenced) but blind to paths
    assembled at runtime; request capture sees exactly what was called, but
    only what the probe's own navigation triggered.
    """
    from playwright.sync_api import sync_playwright

    email = probe_email or PROBE_EMAIL_TEMPLATE.format(rand=str(int(time.time()))[-6:])
    log = TrafficLog()
    with sync_playwright() as p:
        browser = launch_browser(p, headless=headless)
        try:
            context = browser.new_context(no_viewport=True, locale=locale)
            page = context.new_page()
            page.on("request", lambda r: log.add(r.method, r.url))
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)

            if advance:
                mail = page.locator('input[type="email"]').first
                if mail.count() > 0:
                    mail.click()
                    mail.type(email, delay=30)
                    page.wait_for_timeout(400)
                    clicked = False
                    for selector in ('button[type="submit"]', 'button:not([disabled])'):
                        buttons = page.locator(selector)
                        for i in range(buttons.count()):
                            b = buttons.nth(i)
                            try:
                                text = (b.inner_text() or "").strip()
                            except Exception:
                                continue
                            if not text or "google" in text.lower():
                                continue
                            b.click()
                            clicked = True
                            break
                        if clicked:
                            break
                    page.wait_for_timeout(3000)
            return log
        finally:
            browser.close()


def _advance_and_capture(url: str, headless: bool = True, locale: str = "en-US",
                         probe_email: Optional[str] = None):
    """Advance one step and return ``(dom_after, traffic)`` in a single visit."""
    from playwright.sync_api import sync_playwright

    email = probe_email or PROBE_EMAIL_TEMPLATE.format(rand=str(int(time.time()))[-6:])
    log = TrafficLog()
    with sync_playwright() as p:
        browser = launch_browser(p, headless=headless)
        try:
            context = browser.new_context(no_viewport=True, locale=locale)
            page = context.new_page()
            page.on("request", lambda r: log.add(r.method, r.url))
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)

            mail = page.locator('input[type="email"]').first
            if mail.count() == 0:
                return _read_dom(page), log

            mail.click()
            mail.type(email, delay=30)
            page.wait_for_timeout(400)
            clicked = False
            for selector in ('button[type="submit"]', 'button:not([disabled])'):
                buttons = page.locator(selector)
                for i in range(buttons.count()):
                    b = buttons.nth(i)
                    try:
                        text = (b.inner_text() or "").strip()
                    except Exception:
                        continue
                    if not text or "google" in text.lower():
                        continue
                    b.click()
                    clicked = True
                    break
                if clicked:
                    break
            page.wait_for_timeout(2500)
            return _read_dom(page), log
        finally:
            browser.close()


# --------------------------------------------------------------------------
# selector inference
# --------------------------------------------------------------------------

GENERATED_ID_RE = re.compile(r"(_R_|_r_|^[a-z]{1,3}[0-9a-f]{6,}$|[0-9]{4,}$)")


def looks_generated(element_id: str) -> bool:
    """True for ids that are regenerated on every build (e.g. React useId)."""
    if not element_id:
        return False
    return bool(GENERATED_ID_RE.search(element_id))


def infer_selectors(dom: DomInfo) -> Dict[str, str]:
    """Map DOM inputs onto the semantic roles the runner needs.

    Preference order is id > name > data-testid > type, because a text-based
    selector is locale-bound and breaks silently.
    """
    out: Dict[str, str] = {}

    def prefer(el: Dict[str, Any]) -> str:
        # A React `useId` value changes with every build, so an id that looks
        # machine-generated is worse than a plain type selector.
        if el.get("id") and not looks_generated(el["id"]):
            return f"#{el['id']}"
        if el.get("name"):
            return f'input[name="{el["name"]}"]'
        if el.get("testid"):
            return f'[data-testid="{el["testid"]}"]'
        if el.get("type"):
            return f'input[type="{el["type"]}"]'
        return ""

    def pick(cands: List[Dict[str, Any]], want_type: str = "") -> Optional[Dict[str, Any]]:
        pool = [e for e in cands if not want_type or e.get("type") == want_type]
        for key in ("id", "name", "testid", "type"):
            for el in pool:
                if el.get(key):
                    return el
        return None

    email_el = pick(dom.inputs, "email")
    if email_el:
        out["email"] = prefer(email_el) or 'input[type="email"]'

    passwords = [el for el in dom.inputs if el.get("type") == "password"]
    if len(passwords) >= 2:
        out["password"] = prefer(passwords[0]) or 'input[type="password"]'
        out["confirm_password"] = prefer(passwords[1]) or 'input[type="password"]:nth-of-type(2)'
    elif len(passwords) == 1:
        out["password"] = prefer(passwords[0]) or 'input[type="password"]'

    for label in dom.labels:
        text = (label.get("text") or "").lower()
        target = label.get("for")
        if not target:
            continue
        if "otp" in text or "code" in text or "验证码" in text:
            out.setdefault("otp", f"#{target}")
        elif "password" in text or "密码" in text:
            if "confirm" in text or "确认" in text or "再次" in text:
                out.setdefault("confirm_password", f"#{target}")
            else:
                out.setdefault("password", f"#{target}")

    if any(el.get("type") == "submit" for el in dom.buttons):
        out["submit"] = 'button[type="submit"]'
    return out


def infer_steps(dom: DomInfo) -> List[str]:
    steps: List[str] = []
    if any(el.get("type") == "email" for el in dom.inputs):
        steps.append("email")
    if any(el.get("type") == "password" for el in dom.inputs):
        steps.append("password")
    if "otp" in infer_selectors(dom):
        steps.append("otp")
    return steps


def _has_code_field(dom: DomInfo) -> bool:
    """True when the DOM contains something that looks like a code input."""
    return any(
        "otp" in (el.get("id") or "").lower()
        or "code" in (el.get("id") or "").lower()
        or (el.get("name") or "").lower() in ("otp", "code", "verification_code")
        for el in dom.inputs
    )


def infer_verification(scan: Dict[str, Any], dom: DomInfo,
                       endpoints: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Decide whether verification is link-based, code-based, or absent.

    ``endpoints`` are the *composed* paths (base path + fragment).  Without
    them the verification route stays invisible on any site that assembles
    its auth URLs at runtime.
    """
    api_paths = list(scan.get("api_paths") or [])
    samples = list(scan.get("verify_path_samples") or [])
    for role in ("verify", "send-verification"):
        path = (endpoints or {}).get(role)
        if path:
            api_paths.append(path)
    has_otp_input = any(
        "otp" in (el.get("id") or "").lower()
        or "code" in (el.get("id") or "").lower()
        or (el.get("name") or "").lower() in ("otp", "code", "verification_code")
        for el in dom.inputs
    )
    verify_paths = [p for p in api_paths if "verif" in p.lower() or "confirm" in p.lower()]

    if has_otp_input:
        return {
            "mode": "code",
            "sample": verify_paths[0] if verify_paths else "",
            "pattern": r"\b\d{4,8}\b",
            "notes": "an explicit code field was detected",
        }
    if verify_paths or samples:
        sample = verify_paths[0] if verify_paths else samples[0]
        # Derive a default pattern from the endpoint we actually observed, so
        # the verdict has something to match the incoming mail against.
        tail = sample.split("/")[-1]
        pattern = r"https?://[^\s\"'<>\\]*" + re.escape(sample) + r"[^\s\"'<>\\]*"
        return {
            "mode": "link",
            "sample": sample,
            "pattern": pattern,
            "notes": "verification endpoints exist but no code field was found",
        }
    return {"mode": "unknown", "sample": "", "pattern": "",
            "notes": "no verification endpoint or code field was detected"}


def infer_attribution(html: str, invite_url: str,
                      set_cookie_names: Sequence[str] = (),
                      chunk_candidates: Sequence[str] = (),
                      dom_ids: Sequence[str] = ()) -> Dict[str, str]:
    """Figure out how the invite code is carried.

    The authoritative source is the **Set-Cookie header** of the landing
    response: if the server sets an invite/referral cookie, attribution is
    automatic and nothing has to be threaded through the signup request.
    Guessing cookie names out of the HTML is the fallback, not the rule.
    """
    for name in set_cookie_names:
        if "invite" in name.lower() or "referral" in name.lower():
            return {"kind": "cookie", "name": name, "source": "Set-Cookie on the invite landing page"}
    names = re.findall(r'"([A-Za-z0-9_\-]*(?:invite|referral)[A-Za-z0-9_\-]*)"', html, re.I)
    if names:
        return {"kind": "cookie", "name": names[0], "source": invite_url}
    # A quoted string containing "invite" is usually a *field id*
    # ("invite-password"), not a cookie name.  Cross-check against the ids
    # that are actually on the page before believing one.
    known_ids = {i.lower() for i in dom_ids if i}
    field_words = ("password", "confirm", "email", "placeholder", "label", "input")
    for name in chunk_candidates:
        lowered = name.lower()
        if "invite" not in lowered and "referral" not in lowered:
            continue
        if lowered in known_ids:
            continue
        if any(word in lowered for word in field_words):
            continue
        return {"kind": "cookie", "name": name,
                "source": "cookie name referenced by the client bundle"}
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(invite_url).query)
    for key in query:
        if "invite" in key.lower() or "ref" in key.lower():
            return {"kind": "query", "name": key, "source": invite_url}
    return {"kind": "unknown", "name": "", "source": invite_url}


# --------------------------------------------------------------------------
# the top level probe
# --------------------------------------------------------------------------

def probe(
    invite_url: str,
    headless: bool = True,
    locale: str = "en-US",
    advance: bool = True,
    tos_forbids_automation: bool = False,
    timeout: int = 25,
    on_event: Optional[EventFn] = None,
) -> ProbeResult:
    """Run the full discovery pass and return a profile plus a verdict."""
    warnings: List[str] = []
    findings: Dict[str, Any] = {}

    parsed = urllib.parse.urlsplit(invite_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    site = parsed.netloc.replace("www.", "")

    _emit(on_event, "stage", stage="landing", message=f"fetching {invite_url}")
    try:
        r = requests.get(invite_url, timeout=timeout, headers={"User-Agent": USER_AGENT},
                         allow_redirects=True)
        html = r.text
        findings["landing_status"] = r.status_code
        findings["landing_final_url"] = r.url
        findings["landing_set_cookie"] = [c.name for c in r.cookies]
    except Exception as exc:
        html = ""
        warnings.append(f"landing fetch failed: {type(exc).__name__}: {exc}")

    payload = extract_invite_payload(html)
    findings["invite_payload"] = payload
    if payload:
        _emit(on_event, "invite", payload=payload)

    chunks = extract_chunks(html, base_url)
    _emit(on_event, "stage", stage="chunks", message=f"scanning {len(chunks)} bundles")
    scan = scan_chunks(chunks)
    findings["scan"] = scan

    _emit(on_event, "stage", stage="config", message="probing public config")
    cfg = probe_config(base_url)
    findings["config"] = cfg

    _emit(on_event, "stage", stage="dom", message="reading the form")
    dom1 = dom_probe(invite_url, headless=headless, locale=locale)
    findings["dom_initial"] = {"url": dom1.url, "inputs": len(dom1.inputs), "buttons": len(dom1.buttons)}
    dom2: Optional[DomInfo] = None
    traffic: Optional[TrafficLog] = None
    if advance:
        _emit(on_event, "stage", stage="advance",
              message="stepping past the first field and recording traffic")
        # One browser does both jobs: the DOM after advancing, and the request
        # log for the whole visit.  Two separate launches would double the
        # slowest part of the probe.
        dom2, traffic = _advance_and_capture(invite_url, headless=headless, locale=locale)
        if dom2.inputs:
            findings["advanced"] = True
            findings["dom_advanced"] = {"url": dom2.url, "inputs": len(dom2.inputs)}
        else:
            findings["advanced"] = False
        if traffic is not None:
            findings["traffic"] = traffic.to_dict()

    # The two DOMs describe two *different* steps of the same form: the first
    # holds the email field, the second holds the password fields that only
    # render after the existence check.  Merging them (rather than replacing
    # one with the other) is what keeps both sets of selectors.
    selectors: Dict[str, str] = {}
    selectors.update(infer_selectors(dom1))
    if dom2 is not None:
        for key, value in infer_selectors(dom2).items():
            selectors.setdefault(key, value)

    steps = infer_steps(dom1)
    if dom2 is not None:
        for step in infer_steps(dom2):
            if step not in steps:
                steps.append(step)

    captcha = classify_captcha(scan, html, dom1)
    if dom2 is not None:
        deeper = classify_captcha(scan, html, dom2)
        if deeper != "none":
            captcha = deeper

    # Merge what the bundles mention with what the page actually called.
    observed = list(scan.get("api_paths") or [])
    captured = list(traffic.paths) if traffic is not None else []
    merged = list(dict.fromkeys(observed + captured))
    findings["endpoint_sources"] = {
        "from_bundles": len(observed),
        "from_traffic": len(captured),
        "only_in_traffic": sorted(set(captured) - set(observed)),
        "only_in_bundles": sorted(set(observed) - set(captured)),
    }

    endpoints = role_endpoints(merged, scan.get("endpoint_fragments") or [])
    state_endpoints = find_state_endpoints(merged)
    sign_in = find_sign_in_endpoint(merged, scan.get("endpoint_fragments") or [])
    if not state_endpoints:
        warnings.append(
            "no account-state endpoint was found. Endpoints like /api/credits usually "
            "only load after sign-in, so the prober cannot see them. Add them to "
            "endpoints_state by hand if you want the balance reported."
        )

    verification = infer_verification(scan, dom1, endpoints)
    if dom2 is not None and _has_code_field(dom2):
        verification = infer_verification(scan, dom2, endpoints)
    attribution = infer_attribution(
        html, invite_url,
        findings.get("landing_set_cookie") or [],
        scan.get("invite_cookie_candidates") or [],
        [el.get("id") or "" for el in dom1.inputs] + [el.get("id") or "" for el in (dom2.inputs if dom2 else [])],
    )
    invite_code = ""
    for key in ("referralCode", "inviteCode", "code"):
        if payload.get(key):
            invite_code = str(payload[key])
            break
    if not invite_code:
        parts = [p for p in parsed.path.split("/") if p]
        if parts and len(parts[-1]) >= 6 and parts[-1].isalnum():
            invite_code = parts[-1]

    profile = SiteProfile(
        site=site,
        base_url=base_url,
        invite_url=invite_url,
        invite_code=invite_code,
        invite_valid=payload.get("valid") if "valid" in payload else None,
        reward_credits=payload.get("rewardCredits") if "rewardCredits" in payload else None,
        has_reward_slots=payload.get("hasRewardSlots") if "hasRewardSlots" in payload else None,
        inviter_name=payload.get("inviterName", "") or "",
        attribution=Attribution(kind=attribution["kind"], name=attribution["name"],
                                source=attribution["source"]),
        verification=Verification(mode=verification["mode"],
                                  sample=verification.get("sample", ""),
                                  pattern=verification.get("pattern", ""),
                                  notes=verification.get("notes", "")),
        captcha=captcha,
        steps=steps,
        selectors=selectors,
        endpoints=endpoints,
        endpoints_state=state_endpoints,
        sign_in_endpoint=sign_in or "/api/auth/sign-in/email",
        locale=locale,
        probed_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    profile.selector_stability = {k: profile.stability_of(k) for k in profile.selectors}

    # Say which facts are measured and which are inferred, so a human knows
    # where to look.  Everything here is derived from what actually happened
    # during this probe -- nothing is filled in to look complete.
    profile.confidence = {
        "invite_payload": "measured" if payload else "inferred",
        "attribution": "measured" if attribution["kind"] != "unknown" else "inferred",
        "selectors": "measured" if selectors else "inferred",
        "steps": "measured" if steps else "inferred",
        "verification": "measured" if verification["mode"] != "unknown" else "inferred",
        "captcha": "measured" if captcha != "suspected" else "inferred",
        "endpoints": (
            "measured" if traffic is not None and traffic.paths else
            ("inferred" if endpoints else "inferred")
        ),
    }

    if not profile.selectors.get("email"):
        warnings.append("no email input found; the registration form may not start with an email")
    if captcha != "none":
        warnings.append(f"a {captcha} fingerprint was found; the verdict will stop the run")

    verdict = decide(profile, tos_forbids_automation=tos_forbids_automation)
    _emit(on_event, "verdict", verdict=verdict.to_dict())
    return ProbeResult(profile=profile, findings=findings, warnings=warnings, verdict=verdict)
