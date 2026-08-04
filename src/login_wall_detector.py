"""Login-wall and rate-limit detection (FR-4, FR-8).

This module only *detects* restricted content — it never attempts to
bypass login walls, CAPTCHA solvers, etc.
"""

from __future__ import annotations

import re
import time

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from src.browser_utils import implicit_wait_off
from src.logger import get_logger

log = get_logger("login_wall_detector")

# --- Login URL patterns (checked on driver.current_url) ---
_LOGIN_URL_MARKERS = {
    "fb": ("/login.php", "/login/"),
    "ig": ("/accounts/login/",),
}

# --- Login prompt text markers (checked on driver.page_source) ---
_LOGIN_TEXT_MARKERS = {
    # Distinctive "this content requires login" phrases first; "log in" kept
    # as a general fallback but only trusted after the main-content check.
    "fb": (
        "log in to see more",
        "log in to continue",
        "you must log in",
        "log in to view",
        "log in",
    ),
    "ig": (
        "log in to continue",
        "log in to see",
        "you must log in",
        "log in to view",
        "log in",
    ),
}

# --- CSS selectors that indicate real post/comment content has loaded ---
_MAIN_CONTENT_SELECTORS = {
    "fb": (
        '[role="main"]',
        '[role="article"]',
        "div[data-pagelet]",
        "div[aria-label*='comment' i]",
        "form[aria-label*='comment' i]",
    ),
    "ig": (
        "main article",
        "article",
        "div[role='dialog'] ul[role='list']",
        "section main",
    ),
}

_RATE_LIMIT_MARKERS = (
    "you're doing this too often",
    "please slow down",
    "try again later",
    "too many requests",
)
# Only treat 429 as a rate-limit when it appears in an HTTP-status context
# (e.g. `"status": 429`, `HTTP 429`, `429 Too Many Requests`). Scanning a raw
# `\b429\b` over the whole page source is a false-positive magnet because
# Facebook pages embed tons of unrelated numeric data (retry-interval configs,
# JS resource paths, ID arrays). A bare `429` must NOT match.
_RATE_LIMIT_STATUS_RE = re.compile(
    r"HTTP[/\d.]*\s*429|429\s+Too\s+Many\s+Requests|"
    r"[\"']?status(?:_code)?[\"']?\s*[:=]\s*[\"']?429\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """Lowercase and normalize typographic apostrophes to ASCII quotes."""
    return text.lower().replace("\u2019", "'").replace("\u2018", "'")


def _safe_page_source(driver) -> str:
    try:
        return driver.page_source or ""
    except WebDriverException:
        return ""


def _safe_current_url(driver) -> str:
    try:
        return driver.current_url or ""
    except WebDriverException:
        return ""


def _login_url_reason(current_url: str, platform: str) -> str:
    """Return a reason string if the URL redirects to a login page, else ''."""
    markers = _LOGIN_URL_MARKERS.get(platform, ())
    url_lower = _normalize(current_url)
    for marker in markers:
        if marker in url_lower:
            return f"redirected_to_login ({marker})"
    return ""


def _login_marker_in_source(page_source: str, platform: str) -> str:
    """Return the matched login prompt marker, or '' if none found."""
    markers = _LOGIN_TEXT_MARKERS.get(platform, ())
    source_lower = _normalize(page_source)
    for marker in markers:
        if marker in source_lower:
            return marker
    return ""


def _has_main_content(driver, platform: str) -> bool:
    """Return True if the page has loaded real post/comment content.

    Temporarily disables the implicit wait so the check is fast and does
    not stall the detection loop.
    """
    selectors = _MAIN_CONTENT_SELECTORS.get(platform, ())
    if not selectors:
        return False

    original_wait = None
    try:
        original_wait = driver.timeouts.implicit_wait
    except Exception:
        original_wait = None
    if original_wait is not None:
        try:
            driver.implicitly_wait(0)
        except WebDriverException:
            original_wait = None

    try:
        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
            except WebDriverException:
                continue
            if elements:
                return True
        return False
    finally:
        if original_wait is not None:
            try:
                driver.implicitly_wait(original_wait)
            except WebDriverException:
                pass


def detect_login_wall(
    driver,
    platform: str,
    timeout_sec: float = 15.0,
) -> tuple[bool, str]:
    """Detect whether the current page is a login wall.

    Strategy (FR-4):
    1. URL redirected to a login page -> login wall.
    2. Main content present -> NOT a login wall (checked first to avoid
       false positives from a harmless "Log In" button in the footer).
    3. Wait up to ``timeout_sec`` for main content. If a login prompt text
       marker appears without main content -> login wall.
    4. If neither main content nor a login marker appears within the
       timeout -> treat as restricted: ``(True, "no_main_content")``.

    Args:
        driver: active WebDriver.
        platform: ``"fb"`` or ``"ig"``.
        timeout_sec: max seconds to wait for main content.

    Returns:
        ``(is_login_wall: bool, reason: str)``.
    """
    if platform not in ("fb", "ig"):
        return False, ""

    current_url = _safe_current_url(driver)
    reason = _login_url_reason(current_url, platform)
    if reason:
        log.info("Login wall detected via URL: %s", reason)
        return True, reason

    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while time.monotonic() < deadline:
        if _has_main_content(driver, platform):
            return False, ""
        marker = _login_marker_in_source(_safe_page_source(driver), platform)
        if marker:
            log.info("Login wall detected via marker: %r", marker)
            return True, marker
        time.sleep(0.5)

    # Timed out waiting for content.
    if _has_main_content(driver, platform):
        return False, ""
    marker = _login_marker_in_source(_safe_page_source(driver), platform)
    if marker:
        log.info("Login wall detected via marker (after timeout): %r", marker)
        return True, marker

    log.warning("No main content and no login marker after %.1fs; treating as restricted", timeout_sec)
    return True, "no_main_content"


def dismiss_login_popup(driver, platform: str) -> bool:
    """Dismiss a dismissible login/nag popup if one is showing (FR-4).

    Facebook (and sometimes Instagram) overlay a "log in to continue / see
    more" popup on top of content that is actually public — closing the popup
    reveals the post, so it must NOT be treated as a login wall.

    Returns True if a popup was found and closed (caller should re-run
    ``detect_login_wall`` afterwards).  This only dismisses UI overlays — it
    never attempts to bypass a real login wall / authentication flow.
    """
    log.info("Looking for a dismissible login popup (%s)", platform)

    # The dialog scan must not stall on the implicit wait.
    with implicit_wait_off(driver):
        try:
            dialogs = driver.find_elements(By.CSS_SELECTOR, 'div[role="dialog"]')
        except WebDriverException:
            dialogs = []

        if not dialogs:
            return False

        # Close-button labels (EN + ID).
        close_labels = ("close", "tutup", "dismiss")

        for dialog in dialogs:
            try:
                if not dialog.is_displayed():
                    continue
            except WebDriverException:
                continue

            # Only consider dialogs that look like login prompts.
            try:
                text = _normalize((dialog.text or "")[:500])
            except WebDriverException:
                text = ""
            looks_login = any(
                marker in text
                for marker in ("log in", "login", "masuk", "continue", "lanjutkan", "see more")
            )
            if not looks_login:
                continue

            # Prefer an explicit close button (aria-label).
            for label in close_labels:
                try:
                    btns = dialog.find_elements(
                        By.CSS_SELECTOR, f'[aria-label*="{label}" i]'
                    )
                except WebDriverException:
                    btns = []
                for btn in btns:
                    try:
                        if not btn.is_displayed():
                            continue
                    except WebDriverException:
                        continue
                    try:
                        btn.click()
                        log.info("Dismissed login popup via aria-label=%r", label)
                        return True
                    except WebDriverException:
                        try:
                            driver.execute_script("arguments[0].click();", btn)
                            log.info("Dismissed login popup via JS click")
                            return True
                        except WebDriverException:
                            continue

            # Fallback: the dialog's first clickable element that is a close/X icon.
            try:
                icon_btns = dialog.find_elements(
                    By.CSS_SELECTOR,
                    'div[role="button"], button, a[role="button"]',
                )
            except WebDriverException:
                icon_btns = []
            for btn in icon_btns[:6]:
                try:
                    aria = (btn.get_attribute("aria-label") or "").lower()
                    role = (btn.get_attribute("role") or "").lower()
                except WebDriverException:
                    aria = role = ""
                # Close/X buttons usually have a short aria-label or an svg with no text.
                try:
                    btn_text = (btn.text or "").strip()
                except WebDriverException:
                    btn_text = ""
                if (aria and aria in close_labels) or (not btn_text and not aria and role == "button"):
                    try:
                        if not btn.is_displayed():
                            continue
                    except WebDriverException:
                        continue
                    try:
                        btn.click()
                        log.info("Dismissed login popup via icon button")
                        return True
                    except WebDriverException:
                        try:
                            driver.execute_script("arguments[0].click();", btn)
                            log.info("Dismissed login popup via JS icon click")
                            return True
                        except WebDriverException:
                            continue
    return False


def remove_login_popup_and_overlay(driver) -> bool:
    """Delete a login dialog and its dim overlay layer directly from the DOM.

    When expanding comments on public Facebook posts, FB sometimes re-shows a
    "See more on Facebook" login dialog whose gray backdrop hides the comment
    section. Clicking the close button is unreliable there, so this helper
    removes the dialog element and the dark overlay layer with JS ``remove()``,
    revealing the underlying (public) comments.

    Returns True if at least one element was removed.
    """
    script = r"""
      var removed = false;
      // 1) Remove login dialogs (e.g. "See more on Facebook").
      var dialogs = document.querySelectorAll('div[role="dialog"]');
      for (var i = 0; i < dialogs.length; i++) {
        var t = (dialogs[i].textContent || '').toLowerCase();
        if (t.indexOf('log in') >= 0 ||
            t.indexOf('see more on facebook') >= 0 ||
            t.indexOf('email address or phone number') >= 0) {
          dialogs[i].remove();
          removed = true;
        }
      }
      // 2) Remove dark fixed/absolute overlay layers (the gray/black dimming
      //    layer that covers the comments).  Only elements with a near-black
      //    or dark background and a high z-index qualify.
      var all = document.querySelectorAll('div');
      for (var j = 0; j < all.length; j++) {
        var el = all[j];
        if (el.textContent && el.textContent.trim().length > 200) continue;
        var s = window.getComputedStyle(el);
        if (s.position !== 'fixed' && s.position !== 'absolute') continue;
        var z = parseInt(s.zIndex || '0', 10);
        if (isNaN(z) || z < 50) continue;
        var bg = (s.backgroundColor || '').replace(/\s+/g, '');
        var isDark = bg === 'rgb(0,0,0)' || bg === 'rgba(0,0,0,0.5)' ||
                     bg === 'rgba(0,0,0,0.6)' || bg === 'rgba(0,0,0,0.8)';
        if (isDark) {
          el.remove();
          removed = true;
        }
      }
      return removed;
    """
    try:
        removed = bool(driver.execute_script(script))
    except WebDriverException:
        return False
    if removed:
        log.info("Removed login popup dialog and overlay layer(s)")
    return removed


def detect_rate_limit(driver) -> tuple[bool, str]:
    """Detect platform rate-limiting signals in the URL or page source.

    Returns:
        ``(True, marker)`` if a rate-limit signal was found, else ``(False, "")``.
    """
    current_url = _safe_current_url(driver)
    page_source = _safe_page_source(driver)

    text = _normalize(f"{current_url} {page_source}")
    for marker in _RATE_LIMIT_MARKERS:
        if marker in text:
            log.warning("Rate-limit signal detected: %r", marker)
            return True, marker
    if _RATE_LIMIT_STATUS_RE.search(text):
        log.warning("Rate-limit signal detected: HTTP 429")
        return True, "429"
    return False, ""
