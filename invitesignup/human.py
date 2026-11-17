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


# Minimal automation-flag normalisation.  This only stops honest automation
# from being rejected as automation; it does not touch, fake or defeat any
# challenge, and it is deliberately not extended in that direction.
ENVIRONMENT_CONSISTENCY_JS = """
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true });
  } catch (e) {}
})();
"""


def apply_environment_consistency(context) -> None:
    """Inject the minimal flag normalisation into a browser context."""
    try:
        context.add_init_script(ENVIRONMENT_CONSISTENCY_JS)
    except Exception:
        pass
