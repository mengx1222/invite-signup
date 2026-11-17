"""The live-DOM captcha check must only fire on a *visible* challenge.

Captcha vendors leave hidden containers in the DOM.  An existence-only check
would report a challenge on a page that never shows one, contradicting the
verdict's "a bundle keyword is only a suspicion" rule -- the probe would say
"automatable" and the runner would then block itself.

These use a real headless browser against local HTML, so they exercise the
same Playwright visibility semantics the runner relies on.  No network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

playwright = pytest.importorskip("playwright.sync_api")

from invitesignup.runner import _live_captcha  # noqa: E402


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            instance = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            instance = p.chromium.launch(headless=True)
        yield instance
        instance.close()


def _page(browser, html: str):
    context = browser.new_context()
    page = context.new_page()
    page.set_content(html)
    return page


def test_hidden_turnstile_container_is_not_a_challenge(browser):
    page = _page(browser, '<div id="cf-turnstile-abc" style="display:none"></div>')
    assert _live_captcha(page) == ""


def test_zero_size_captcha_wrapper_is_not_a_challenge(browser):
    page = _page(browser, '<div class="captcha-wrapper" style="width:0;height:0;overflow:hidden"></div>')
    assert _live_captcha(page) == ""


def test_visible_turnstile_iframe_is_detected(browser):
    page = _page(
        browser,
        '<iframe src="https://challenges.cloudflare.com/turnstile/v0/api.js" '
        'style="width:300px;height:65px"></iframe>',
    )
    assert _live_captcha(page) == "turnstile"


def test_visible_hcaptcha_iframe_is_detected(browser):
    page = _page(
        browser,
        '<iframe src="https://newassets.hcaptcha.com/captcha/v1/x" '
        'style="width:300px;height:65px"></iframe>',
    )
    assert _live_captcha(page) == "hcaptcha"


def test_clean_page_has_no_challenge(browser):
    page = _page(browser, "<form><input type='email'></form>")
    assert _live_captcha(page) == ""
