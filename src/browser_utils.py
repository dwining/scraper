"""Browser/driver setup and config loading utilities.

Implements FR-2: non-headless Chrome via ``webdriver-manager``, randomized
window size, a real desktop user-agent, and explicit timeouts driven by
``config.yaml``.
"""

from __future__ import annotations

import random
from contextlib import contextmanager
from pathlib import Path

import yaml
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from src.logger import get_logger

log = get_logger("browser_utils")

# A real, common desktop Chrome user-agent string (recent Chrome).
# Kept consistent with the window sizes we pick (desktop viewport).
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_WINDOW_SIZES = [(1366, 768), (1440, 900), (1920, 1080)]
DEFAULT_IMPLICIT_WAIT = 10.0
DEFAULT_PAGE_LOAD_TIMEOUT = 60.0


def load_config(config_path: str) -> dict:
    """Load ``config.yaml`` and return it as a dict.

    Raises:
        FileNotFoundError: if the config file does not exist.
        ValueError: if the YAML does not parse into a mapping.
    """
    path = Path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ValueError(f"Config file {config_path} did not contain a YAML mapping")
    return cfg


def pick_window_size(config: dict) -> tuple[int, int]:
    """Pick a random window size from ``config["browser"]["window_size_options"]``."""
    browser_cfg = config.get("browser") or {}
    raw_options = browser_cfg.get("window_size_options") or DEFAULT_WINDOW_SIZES
    if not raw_options:
        raw_options = DEFAULT_WINDOW_SIZES
    choice = random.choice(raw_options)
    try:
        return (int(choice[0]), int(choice[1]))
    except (TypeError, IndexError, ValueError):
        log.warning("Malformed window_size_options entry %r; using default", choice)
        return (1366, 768)


def setup_driver(config: dict, headless: bool = False) -> webdriver.Chrome:
    """Create and configure a Chrome WebDriver.

    - Non-headless by default (browser testing mode); ``headless`` only for
      debugging.
    - Uses ``webdriver-manager`` to install/match the ChromeDriver binary.
    - Sets a real desktop user-agent and disables the automation infobar.
    - Picks a random window size from config and applies it explicitly.
    - Applies implicit wait and page-load timeout from config.

    Args:
        config: full config dict (from :func:`load_config`).
        headless: if True, adds ``--headless=new``.

    Returns:
        A ready-to-use ``webdriver.Chrome`` instance.
    """
    browser_cfg = config.get("browser") or {}

    options = Options()
    options.add_argument(f"--user-agent={DEFAULT_USER_AGENT}")
    # Reduce "automation" signals without bypassing any security mechanism.
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-notifications")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    if headless:
        options.add_argument("--headless=new")

    width, height = pick_window_size(config)
    options.add_argument(f"--window-size={width},{height}")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )

    # Explicit window size on the live driver (not just the launch arg).
    try:
        driver.set_window_size(width, height)
    except Exception as exc:  # defensive: sizing should not kill the session
        log.warning("Could not set window size %dx%d: %s", width, height, exc)

    try:
        implicit_wait = float(browser_cfg.get("implicit_wait_sec") or DEFAULT_IMPLICIT_WAIT)
        page_load_timeout = float(
            browser_cfg.get("page_load_timeout_sec") or DEFAULT_PAGE_LOAD_TIMEOUT
        )
    except (TypeError, ValueError):
        implicit_wait = DEFAULT_IMPLICIT_WAIT
        page_load_timeout = DEFAULT_PAGE_LOAD_TIMEOUT

    driver.implicitly_wait(implicit_wait)
    driver.set_page_load_timeout(page_load_timeout)

    log.info(
        "Chrome driver ready (window=%dx%d, headless=%s, implicit_wait=%ss, page_load_timeout=%ss)",
        width, height, headless, implicit_wait, page_load_timeout,
    )
    return driver


@contextmanager
def implicit_wait_off(driver):
    """Temporarily disable the implicit wait for fast bulk DOM scans.

    Selenium's implicit wait makes *every* ``find_element(s)`` call that does
    not match block for the full wait interval. Bulk scans over large pages
    (e.g. walking many ``div[role="article"]`` comment wrappers) would then
    stall for minutes. This context manager zeroes the wait during the scan
    and restores it afterwards; the scrapers already add explicit
    ``sleep_random`` pauses where content needs time to appear.
    """
    original = None
    try:
        original = driver.timeouts.implicit_wait
    except Exception:
        original = None
    if original:
        try:
            driver.implicitly_wait(0)
        except Exception:
            original = None
    try:
        yield
    finally:
        if original is not None:
            try:
                driver.implicitly_wait(original)
            except Exception:
                pass
