"""Human-ish interaction helpers.

These exist so that a legitimate flow is not rejected for looking like a
script that types in 3ms flat.  They do NOT attempt to defeat any challenge:
if a site shows a captcha, the verdict stops the run before we get here.
"""

from __future__ import annotations

import random
import time
from typing import Union

from playwright.sync_api import Locator, Page


def human_type(page: Page, target: Union[str, Locator], text: str,
               min_delay: float = 0.03, max_delay: float = 0.09) -> None:
    """Type character by character with a slightly irregular cadence.

    ``press_sequentially`` is used on purpose.  It was checked against a real
    password containing ``+ - ! @ # $ % & * _ =`` and reproduces the string
    exactly, while also emitting ``keydown`` / ``keypress`` per character.
    ``insert_text`` would insert the same text but fires only one ``keydown``
    for the whole string, which is *less* like a human typing -- so it is
    deliberately not used here.
    """
    loc = page.locator(target) if isinstance(target, str) else target
    loc.wait_for(state="visible", timeout=15000)
    loc.scroll_into_view_if_needed()
    loc.click()
    time.sleep(random.uniform(0.08, 0.18))
    loc.fill("")
    time.sleep(random.uniform(0.04, 0.09))
    for char in text:
        loc.press_sequentially(char, delay=int(random.uniform(min_delay, max_delay) * 1000))
        if random.random() < 0.07:
            time.sleep(random.uniform(0.08, 0.22))
    time.sleep(random.uniform(0.08, 0.18))


def human_click(page: Page, target: Union[str, Locator], timeout: int = 15000) -> bool:
    """Hover, wait until enabled, then click."""
    loc = page.locator(target) if isinstance(target, str) else target
    try:
        loc.wait_for(state="visible", timeout=timeout)
        loc.scroll_into_view_if_needed()
        deadline = time.time() + timeout / 1000.0
        while time.time() < deadline:
            if loc.is_enabled():
                break
            time.sleep(0.25)
        box = loc.bounding_box()
        if box:
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            time.sleep(random.uniform(0.04, 0.12))
        loc.click(timeout=timeout)
        return True
    except Exception:
        return False


def click_first_enabled_non_google(page: Page) -> bool:
    """Click the first enabled button that is not a social sign-in button."""
    for selector in ('button[type="submit"]', 'button:not([disabled])'):
        buttons = page.locator(selector)
        for i in range(buttons.count()):
            button = buttons.nth(i)
            try:
                text = (button.inner_text() or "").strip()
            except Exception:
                continue
            if not text or "google" in text.lower():
                continue
            if human_click(page, button):
                return True
    return False


# Launch flags that keep honest automation from being rejected *as* automation.
#
# Measured on this machine: with `--disable-blink-features=AutomationControlled`
# and `--enable-automation` removed, `navigator.webdriver` is already the native
# `false` getter (`function get webdriver() { [native code] }`).  An earlier
# version also injected `Object.defineProperty(navigator, 'webdriver', ...)` as
# an init script.  That was measured to have *no effect* -- the prototype's
# native accessor wins -- so it was dead code that looked like a safeguard.
# It has been removed rather than left in place to mislead the next reader.
LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled", "--no-first-run")
IGNORE_DEFAULT_ARGS = ("--enable-automation",)


def launch_browser(playwright, *, headless: bool = True):
    """Launch the browser with one canonical set of flags.

    Three call sites used to carry their own copy of these flags.  Changing a
    flag in one place and missing the other two is the kind of drift that only
    shows up as an unexplained behavioural difference between probe and run.

    Prefer the locally installed Chrome, but fall back to Playwright's bundled
    Chromium.  README tells new users that `playwright install chromium` is
    enough; without the fallback that setup document was a trap on any machine
    without Google Chrome.  The browser test already did this fallback, so
    CI stayed green while the product path would not start.
    """
    launch_kwargs = {
        "headless": headless,
        "args": list(LAUNCH_ARGS),
        "ignore_default_args": list(IGNORE_DEFAULT_ARGS),
    }
    try:
        return playwright.chromium.launch(channel="chrome", **launch_kwargs)
    except Exception:
        # If Chrome is absent, use the browser Playwright actually installed.
        # Do not inspect the exception text: launch failures have many messages
        # and the fallback is safe even when Chrome failed for another reason.
        return playwright.chromium.launch(**launch_kwargs)


def apply_environment_consistency(context) -> None:
    """Kept as a no-op hook for callers.

    There is nothing to inject: the launch flags already produce a native
    `webdriver === false`, and injecting a fake getter on top was measured to
    change nothing while adding a detectable non-native property.
    """
    return None
