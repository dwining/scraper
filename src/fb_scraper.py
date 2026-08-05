"""Facebook public-post comment scraper (``FbScraper``).

Implements PRD §5 FR-4 (login-wall abort), FR-5 (comment extraction) and the
§7 output schema.  The caller (``main.py``) owns retries and file persistence;
this module only loads the page, expands/collects comments and returns the
result dict.

SELECTOR NOTES (PRD §13 manual test plan):
    Facebook's DOM is unstable and changes frequently.  The selectors below
    cover the current (2025+) FB comment DOM and the legacy structures:

      * post caption     -> ``[data-ad-preview="message"]`` (fallback: long
                            ``span[dir="auto"]`` text / first ``div[dir="auto"]``
                            inside the post ``div[role="article"]``)
      * comment wrapper  -> ``div[aria-label^="Comment by" i]`` (current DOM;
                            the aria-label looks like
                            "Comment by <Author> <relative time>") or legacy
                            ``div[role="article"]`` / ``div.UFICommentContent``
      * comment author   -> first ``span[dir="auto"]`` (current DOM; the old
                            ``user.php``/``profile.php`` author links are gone,
                            so ``author_profile_url`` is left None)
      * comment text     -> ``div[dir="auto"]`` (mirrored in a second
                            ``span[dir="auto"]``); legacy fallback
                            ``div[data-ad-comment-text]``
      * comment time     -> ``a`` element whose text is a relative time
                            (e.g. "3w", "2j", "5 hari") -> ``comment_time_raw``;
                            legacy ``abbr[data-utime]`` / ``abbr[title]``
      * like count       -> trailing numeric token in the wrapper text
                            (best effort, only 1..99999); legacy
                            ``[data-testid="UFICommentLikeCount"]``
      * expand button    -> ``[data-testid="UFICommentLink"]`` / class
                            ``UFICommentLink`` and link/button text matching
                            ``view more comments`` / ``lihat ... komentar``
      * sort filter      -> ``div[role="button"]`` labeled "Most relevant"
                            opens a ``div[role="menu"]``; click the
                            ``div[role="menuitem"]`` whose text starts with
                            "All comments" so the scraper sees every comment
      * reply expand     -> text matching ``view all N replies`` on a
                            ``div[role="button"]`` / ``a`` / ``span[role="button"]``;
                            clicking reveals the reply wrappers below (and can
                            re-trigger the login dialog, which is removed)
      * reply wrapper    -> ``div[aria-label^="Reply by" i]`` with the same
                            field structure as comments; flagged via
                            ``is_reply`` / ``parent_comment_id``
      * comment ids      -> the author link's base64 ``comment_id`` query param
                            decodes to "comment:<a>_<b>"; replies:
                            parent_comment_id=<a>, comment_id=<b or a>;
                            top-level comments: comment_id=<b or a>

    These selectors may need manual adjustment when FB changes their markup.
"""

import base64
import json
import random
import re
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By

from src.exceptions import ScrapeError, LoginRequiredError, RateLimitError
from src.human_behavior import (
    sleep_random,
    human_move_to,
    human_click,
    human_scroll,
    look_around,
)
from src.browser_utils import implicit_wait_off
from src.login_wall_detector import (
    detect_login_wall,
    detect_rate_limit,
    dismiss_login_popup,
    remove_login_popup_and_overlay,
)
from src.io_utils import extract_post_id
from src.logger import get_logger

__all__ = ["FbScraper"]


class FbScraper:
    """Scrapes comments from a public Facebook post with a given WebDriver."""

    # --- Selector / regex constants (see module docstring) --------------- #
    # Post caption.
    CAPTION_SELECTORS = (
        '[data-ad-preview="message"]',
        'div[data-ad-preview="message"]',
    )
    # Comment text elements.
    COMMENT_TEXT_SELECTORS = (
        'div[data-ad-comment-text]',
        'span[data-ad-comment-text]',
    )
    # Comment wrapper fallbacks.
    COMMENT_WRAPPER_SELECTORS = (
        'div.UFICommentContent',
    )
    # Modern FB: every comment wrapper carries aria-label="Comment by <Author>
    # <relative time>".  Case-insensitive CSS attribute prefix match.
    MODERN_COMMENT_SELECTOR = 'div[aria-label^="Comment by" i]'
    # Comment author links.
    AUTHOR_LINK_SELECTORS = (
        'a[data-hovercard*="user.php"]',
        'a[data-hovercard*="profile.php"]',
        'a[href*="user.php"]',
        'a[href*="profile.php"]',
    )
    # "View more comments" controls (attribute based).
    MORE_BUTTON_SELECTORS = (
        'div[data-testid="UFICommentLink"]',
        'a[data-testid="UFICommentLink"]',
        'span[data-testid="UFICommentLink"]',
        'div[class*="UFICommentLink"]',
        'a[class*="UFICommentLink"]',
    )
    # ... and text based (English + Indonesian).
    MORE_BUTTON_TEXT_RE = re.compile(
        r"(?i)(view\s+more\s+comments|lihat\s+.*\s+komentar|lihat\s+komentar)"
    )
    RELATIVE_TIME_RE = re.compile(
        r"(?i)(\d+\s*(detik|menit|jam|hari|minggu|bulan|tahun|[smhdwj]))"
    )
    LIKE_COUNT_RE = re.compile(r"([\d.,]+)\s*(suka|like)", re.IGNORECASE)
    REPLY_COUNT_RE = re.compile(r"([\d.,]+)\s*(balasan|replies|reply)", re.IGNORECASE)
    # Reply expansion: "View all 2 replies" buttons that reveal nested replies.
    REPLY_EXPAND_TEXT_RE = re.compile(r"(?i)view\s+all\s+\d+\s+replies")
    # Modern FB: nested reply wrappers carry aria-label="Reply by <Author> ...".
    REPLY_WRAPPER_SELECTOR = 'div[aria-label^="Reply by" i]'
    # Decoded "comment_id" query param looks like "comment:<a>_<b>"
    # (or "comment:<a>" when the reply id is absent).
    REPLY_COMMENT_ID_RE = re.compile(r"comment:(\d+)(?:_(\d+))?")

    # Safety cap for the expand/collect loop (FR-5).
    MAX_EXPAND_ITERATIONS = 25

    def __init__(self, driver, config: dict, on_progress=None):
        """driver: a Selenium WebDriver; config: dict from load_config()."""
        self.driver = driver
        self.config = config
        self.logger = get_logger("fb_scraper")
        self.use_replies = False
        self.on_progress = on_progress

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def scrape_post(self, url: str) -> dict:
        """Open ``url``, expand + collect comments, return the PRD §7 dict.

        Raises:
            LoginRequiredError: the post is private / requires login (skip, no retry).
            RateLimitError:     FB rate-limiting detected (stop the batch).
            ScrapeError:        page-level fatal error (caller may retry).
        """
        self.logger.info("Scraping FB post: %s", url)
        between = self.config["delays"]["between_actions_sec"]
        try:
            try:
                self.driver.get(url)
            except Exception as exc:
                raise ScrapeError(f"Could not load FB URL {url}: {exc}") from exc
            sleep_random(sec_range=between)

            # Dismiss any dismissible login/nag popup first — FB often overlays
            # a "log in to see more" popup on PUBLIC content; closing it reveals
            # the post.  Real login walls are still caught below.
            if dismiss_login_popup(self.driver, "fb"):
                sleep_random(sec_range=(1, 2))

            # FR-4: abort on login wall / private content.
            login_wall, reason = detect_login_wall(
                self.driver,
                "fb",
                timeout_sec=self.config["scraping"]["comments_wait_sec"],
            )
            if login_wall:
                raise LoginRequiredError(reason or "FB login wall detected")

            # FR-8: abort the batch if FB is rate-limiting us.
            rate_limited, marker = detect_rate_limit(self.driver)
            if rate_limited:
                raise RateLimitError(marker or "FB rate-limit marker detected")

            post_caption = self._extract_post_caption()

            # Switch the comment sort filter to "All comments" so the scraper
            # sees every comment, not only the "Most relevant" subset.  Give
            # the comment list a moment to reload when the switch happened.
            if self._select_all_comments_filter():
                sleep_random(sec_range=(1, 2))

            max_comments = self._max_comments()
            comments = self._load_and_collect_comments(url, post_caption, max_comments)

            scraped_at = datetime.now().astimezone()
            result = {
                "post_id": extract_post_id(url, "fb"),
                "platform": "FB",
                "post_url": url,
                "scraped_at": scraped_at.isoformat(timespec="seconds"),
                "post_caption": post_caption,
                "total_comments_scraped": len(comments),
                "comments": comments,
            }
            self.logger.info("FB post %s done: %d comments", url, len(comments))
            return result
        except (LoginRequiredError, RateLimitError, ScrapeError):
            raise
        except Exception as exc:  # defensive: any other fatal error
            raise ScrapeError(
                f"Fatal error while scraping FB post {url}: {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Expand + collect
    # ------------------------------------------------------------------ #
    def _max_comments(self) -> Optional[int]:
        """Batas komentar per postingan; None berarti tak terbatas (0)."""
        try:
            value = int(self.config["scraping"]["max_comments_per_post"])
        except Exception:
            value = 0
        return value if value > 0 else None

    def _load_and_collect_comments(
        self,
        url: str,
        caption: Optional[str],
        max_comments: Optional[int],
    ) -> list:
        """Expand and collect comments with human-like pacing.

        Loop: collect currently rendered comments -> check stop conditions ->
        find/click the "view more comments" control -> scroll around.

        Stops when: no more expand button is found, ``max_comments`` is reached,
        the safety iteration cap is hit, or expansion stalls.  A rate-limit
        marker checked between iterations raises RateLimitError (FR-8).
        """
        comments = []
        seen = set()
        between = self.config["delays"]["between_actions_sec"]
        scroll_step = tuple(
            self.config.get("delays", {}).get("scroll_step_px", (200, 600))
        )
        look_prob = float(
            self.config.get("delays", {}).get("look_around_probability", 0.3)
        )

        prev_button_id = None
        stalled = 0

        # Bulk DOM scans must not stall on the implicit wait; the explicit
        # sleep_random pauses below give lazy-loaded content time to appear.
        with implicit_wait_off(self.driver):
            # Expand collapsed reply threads once up-front (the "All comments"
            # filter has already been applied by the caller) so reply wrappers
            # ("Reply by ...") are visible when collection starts.  Only done
            # when ``use_replies`` is enabled (default: top-level comments).
            if self.use_replies:
                self._expand_replies()
            for iteration in range(1, self.MAX_EXPAND_ITERATIONS + 1):
                # Rate-limit re-check between iterations (FR-8).
                rate_limited, marker = detect_rate_limit(self.driver)
                if rate_limited:
                    raise RateLimitError(
                        marker or "FB rate-limit detected during comment expansion"
                    )

                before = len(comments)
                limit = None if max_comments is None else max_comments - len(comments)
                for item in self._collect_visible_comments(limit):
                    key = item["comment_id"] or (item["author_name"], item["comment_text"])
                    if key in seen:
                        continue
                    seen.add(key)
                    comments.append(item)
                    self._emit_progress(url, caption, comments)
                newly_added = len(comments) - before
                self.logger.info(
                    "expansion iteration %d: +%d new (total %d)",
                    iteration, newly_added, len(comments),
                )
                self._emit_progress(
                    url,
                    caption,
                    comments,
                    progress={
                        "iterations": iteration,
                        "max_iterations": self.MAX_EXPAND_ITERATIONS,
                        "new_comments": newly_added,
                    },
                )

                if max_comments is not None and len(comments) >= max_comments:
                    self.logger.info(
                        "Reached max_comments_per_post=%d", max_comments
                    )
                    break

                button = self._find_more_comments_button()
                if button is None:
                    self.logger.info("No more 'view more comments' button found")
                    break

                # Stall guard: same button + no new comments -> clicking won't help.
                try:
                    button_id = button.id
                except Exception:
                    button_id = None
                if button_id == prev_button_id and newly_added == 0:
                    stalled += 1
                    if stalled >= 2:
                        self.logger.info("Comment expansion stalled; stopping")
                        break
                else:
                    stalled = 0
                prev_button_id = button_id

                if not self._click_more_button(button):
                    self.logger.warning(
                        "Could not click 'more comments' button; stopping"
                    )
                    break

                sleep_random(sec_range=(0.5, 1.5))

                # Expanding comments often re-triggers a "See more on Facebook"
                # login dialog whose gray overlay hides the comment section.
                # Delete the popup and the dark overlay layer directly from the
                # DOM so the (public) comments are revealed again.
                if remove_login_popup_and_overlay(self.driver):
                    sleep_random(sec_range=(1, 2))

                sleep_random(sec_range=between)

                # Natural behavior: occasional look-around + staged scrolling.
                if random.random() < look_prob:
                    look_around(self.driver)
                human_scroll(self.driver, step_px_range=scroll_step)
                sleep_random(sec_range=between)

        return comments

    def _emit_progress(self, url: str, caption: Optional[str],
                       comments: list, progress: Optional[dict] = None) -> None:
        """Kirim hasil parsial ke callback on_progress (save JSON live)."""
        if self.on_progress is None:
            return
        try:
            partial = {
                "post_id": extract_post_id(url, "fb"),
                "platform": "FB",
                "post_url": url,
                "scraped_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "post_caption": caption,
                "total_comments_scraped": len(comments),
                "comments": comments,
            }
            if progress is not None:
                partial["progress"] = progress
            self.on_progress(partial)
        except Exception:
            pass  # kegagalan progress save tidak boleh menggagalkan scrape

    def _click_more_button(self, button) -> bool:
        """Click an expand control using human-like interactions + JS fallback."""
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                button,
            )
            sleep_random(sec_range=(1, 3))
        except Exception:
            pass
        try:
            human_move_to(self.driver, button)
        except Exception as exc:
            self.logger.debug("human_move_to failed: %s", exc)
        try:
            human_click(self.driver, button)
            return True
        except Exception as exc:
            self.logger.warning("human_click failed (%s); trying JS click", exc)
        try:
            self.driver.execute_script("arguments[0].click();", button)
            return True
        except Exception as exc:
            self.logger.warning("JS fallback click failed: %s", exc)
            return False

    def _expand_replies(self) -> None:
        """Expand collapsed reply threads ("View all N replies" buttons).

        Best-effort loop (capped at ``MAX_EXPAND_ITERATIONS``): find a
        displayed element whose text matches ``REPLY_EXPAND_TEXT_RE``, click it
        the same way ``_click_more_button`` does, then clear any re-triggered
        "See more on Facebook" login dialog before re-scanning.  Newly loaded
        ``div[aria-label^="Reply by" i]`` wrappers become part of the visible
        wrapper set on the next collect pass.
        """
        with implicit_wait_off(self.driver):
            for _ in range(self.MAX_EXPAND_ITERATIONS):
                # Find a displayed reply-expand control.
                button = None
                try:
                    candidates = self.driver.find_elements(
                        By.CSS_SELECTOR,
                        'div[role="button"], a, span[role="button"]',
                    )
                except Exception:
                    candidates = []
                for el in candidates:
                    try:
                        text = (el.text or "").strip()
                    except Exception:
                        text = ""
                    if (text and len(text) <= 60
                            and self.REPLY_EXPAND_TEXT_RE.search(text)
                            and self._is_displayed(el)):
                        button = el
                        break
                if button is None:
                    return

                self.logger.info(
                    "Expanding replies (button %r)", (button.text or "")[:60]
                )

                # Click like _click_more_button (scrollIntoView + human_move_to
                # + human_click, JS fallback), but without its long pre/post
                # sleeps.
                try:
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                        button,
                    )
                    sleep_random(sec_range=(0.5, 1.5))
                except Exception:
                    pass
                # JS click is the reliable method for reply-expand: the button
                # is usually covered by the login popup overlay, which makes
                # native/human clicks land on the overlay instead (the click
                # "succeeds" silently and no replies are expanded).
                clicked = False
                try:
                    self.driver.execute_script("arguments[0].click();", button)
                    clicked = True
                except Exception as exc:
                    self.logger.warning("JS click on reply-expand failed: %s", exc)
                    try:
                        human_move_to(self.driver, button)
                    except Exception as exc2:
                        self.logger.debug("human_move_to failed: %s", exc2)
                    try:
                        human_click(self.driver, button)
                        clicked = True
                    except Exception as exc2:
                        self.logger.warning(
                            "human_click on reply-expand failed: %s", exc2
                        )
                if not clicked:
                    return

                sleep_random(sec_range=(0.5, 1.5))

                # Expanding replies can re-trigger the "See more on Facebook"
                # login dialog whose gray overlay hides the comments; remove it.
                if remove_login_popup_and_overlay(self.driver):
                    sleep_random(sec_range=(1, 2))

    def _select_all_comments_filter(self) -> bool:
        """Switch the comment sort dropdown from "Most relevant" to "All comments".

        Best-effort: FB's comment section shows a sort/filter dropdown labeled
        "Most relevant"; without switching, only the "most relevant" subset of
        comments is loaded.  This opens that dropdown and clicks the
        "All comments" menuitem so the scraper sees every comment.

        Returns:
            True if the "All comments" menuitem was clicked, False otherwise
            (the default filter is fine when the control is absent).
        """
        try:
            with implicit_wait_off(self.driver):
                # 1) Find the dropdown button: a div[role="button"] whose
                #    visible text is exactly "Most relevant".
                button = None
                try:
                    candidates = self.driver.find_elements(
                        By.CSS_SELECTOR, 'div[role="button"]'
                    )
                except Exception:
                    candidates = []
                for el in candidates:
                    try:
                        text = (el.text or "").strip()
                    except Exception:
                        text = ""
                    if text == "Most relevant" and self._is_displayed(el):
                        button = el
                        break
                if button is None:
                    self.logger.info(
                        "Comment sort filter 'Most relevant' not found; "
                        "keeping the default filter"
                    )
                    return False

                # 2) Open the dropdown (scroll into view + human-like click,
                #    JS fallback).
                try:
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                        button,
                    )
                    sleep_random(sec_range=(0.5, 1.5))
                except Exception:
                    pass
                try:
                    human_move_to(self.driver, button)
                except Exception as exc:
                    self.logger.debug("human_move_to failed: %s", exc)
                try:
                    human_click(self.driver, button)
                except Exception as exc:
                    self.logger.warning(
                        "human_click on sort dropdown failed (%s); trying JS click",
                        exc,
                    )
                    try:
                        self.driver.execute_script("arguments[0].click();", button)
                    except Exception as exc2:
                        self.logger.warning(
                            "JS click on sort dropdown failed: %s", exc2
                        )
                        return False

                # 3) Wait for the menu, then click the "All comments" item.
                sleep_random(sec_range=(1.2, 1.8))
                try:
                    menu = self.driver.find_element(
                        By.CSS_SELECTOR, 'div[role="menu"]'
                    )
                except Exception:
                    self.logger.warning(
                        "Sort dropdown menu not found after opening"
                    )
                    return False
                try:
                    items = menu.find_elements(
                        By.CSS_SELECTOR, 'div[role="menuitem"]'
                    )
                except Exception:
                    items = []
                target = None
                for item in items:
                    try:
                        text = (item.text or "").strip()
                    except Exception:
                        text = ""
                    if text.startswith("All comments") and self._is_displayed(item):
                        target = item
                        break
                if target is None:
                    self.logger.warning(
                        "'All comments' menuitem not found in the sort dropdown"
                    )
                    return False

                try:
                    human_move_to(self.driver, target)
                except Exception as exc:
                    self.logger.debug("human_move_to failed: %s", exc)
                try:
                    human_click(self.driver, target)
                    self.logger.info("Switched comment filter to 'All comments'")
                    return True
                except Exception as exc:
                    self.logger.warning(
                        "human_click on 'All comments' failed (%s); trying JS click",
                        exc,
                    )
                try:
                    self.driver.execute_script("arguments[0].click();", target)
                    self.logger.info("Switched comment filter to 'All comments' (JS)")
                    return True
                except Exception as exc:
                    self.logger.warning(
                        "JS click on 'All comments' failed: %s", exc
                    )
                    return False
        except Exception as exc:  # never crash the run
            self.logger.warning("Failed to select 'All comments' filter: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    # Comment collection (defensive per-element)
    # ------------------------------------------------------------------ #
    def _collect_visible_comments(self, limit: Optional[int]) -> list:
        """Find currently rendered comment wrappers and parse each one.

        Every wrapper is parsed independently; a single malformed element is
        skipped and never crashes the whole run.
        """
        wrappers = self._find_comment_wrappers()
        items = []
        for wrapper in wrappers:
            if limit is not None and len(items) >= limit:
                break
            try:
                item = self._parse_comment(wrapper)
            except Exception as exc:
                self.logger.warning("Skipping malformed comment element: %s", exc)
                continue
            if not item["comment_text"] and not item["author_name"]:
                continue
            if not self.use_replies and item["is_reply"]:
                continue
            items.append(item)
        return items

    def _find_comment_wrappers(self) -> list:
        """Collect unique comment wrapper elements (deduped by element id).

        Primary: comment-text elements (``div[data-ad-comment-text]``) mapped
        to their nearest ``div[role="article"]`` ancestor (or
        ``div.UFICommentContent`` / the text element itself as fallback).
        Secondary: standalone ``div[role="article"]`` elements that look like
        comments.
        """
        wrappers = []
        seen_ids = set()

        def add_wrapper(el):
            if el is None:
                return
            try:
                el_id = el.id
            except Exception:
                return
            if el_id in seen_ids:
                return
            seen_ids.add(el_id)
            wrappers.append(el)

        # Primary pass (modern FB): every comment wrapper carries an
        # aria-label like "Comment by <Author> <relative time>".  Add each
        # displayed match directly (they are already the comment wrappers).
        try:
            modern_wrappers = self.driver.find_elements(
                By.CSS_SELECTOR, self.MODERN_COMMENT_SELECTOR
            )
        except Exception:
            modern_wrappers = []
        for el in modern_wrappers:
            if self._is_displayed(el):
                add_wrapper(el)

        # Primary pass (modern FB replies): reply wrappers carry
        # aria-label="Reply by <Author> to ...".  Added directly so
        # _parse_comment can flag them as replies via their aria-label.
        # Skipped entirely when ``use_replies`` is disabled (default):
        # only top-level comments are collected.
        if self.use_replies:
            try:
                reply_wrappers = self.driver.find_elements(
                    By.CSS_SELECTOR, self.REPLY_WRAPPER_SELECTOR
                )
            except Exception:
                reply_wrappers = []
            for el in reply_wrappers:
                if self._is_displayed(el):
                    add_wrapper(el)

        # Primary pass (legacy): elements that definitely hold comment text.
        for selector in self.COMMENT_TEXT_SELECTORS:
            try:
                text_els = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for el in text_els:
                if not self._is_displayed(el):
                    continue
                add_wrapper(self._wrapper_for_text_element(el))

        # Secondary pass: role=article wrappers that look like comments.
        try:
            articles = self.driver.find_elements(
                By.CSS_SELECTOR, 'div[role="article"]'
            )
        except Exception:
            articles = []
        first_article_id = articles[0].id if articles else None
        for article in articles:
            if not self._is_displayed(article):
                continue
            if self._is_comment_article(article, first_article_id):
                add_wrapper(article)

        return wrappers

    def _wrapper_for_text_element(self, el):
        """Return the nearest usable wrapper for a comment-text element."""
        try:
            return el.find_element(
                By.XPATH, "./ancestor::div[@role='article'][1]"
            )
        except NoSuchElementException:
            pass
        except Exception:
            pass
        try:
            return el.find_element(
                By.XPATH,
                "./ancestor::div[contains(@class,'UFICommentContent')][1]",
            )
        except Exception:
            pass
        return el

    def _is_comment_article(self, article, first_article_id) -> bool:
        """Heuristic: is this role=article element a comment (not the post)?"""
        # Modern FB: aria-label="Comment by <Author> <relative time>".
        try:
            aria = (article.get_attribute("aria-label") or "").lower()
            if aria.startswith("comment by"):
                return True
        except Exception:
            pass
        try:
            cls = article.get_attribute("class") or ""
        except Exception:
            cls = ""
        if "UFIComment" in cls or "UFIReply" in cls:
            return True
        try:
            if article.find_elements(By.CSS_SELECTOR, "div[data-ad-comment-text]"):
                return True
        except Exception:
            pass
        # The post article is usually the first role=article; exclude it.
        try:
            if first_article_id is not None and article.id == first_article_id:
                return False
        except Exception:
            pass
        # Comments live inside the comments section / comment lists.
        try:
            article.find_element(
                By.XPATH,
                "./ancestor::div[@aria-label='Comments'][1] | "
                "./ancestor::div[contains(@data-testid,'comment')][1] | "
                "./ancestor::div[contains(@class,'UFICommentList')][1]",
            )
            return True
        except NoSuchElementException:
            pass
        except Exception:
            pass
        # Author-link heuristic (excluding the post, which carries the caption).
        try:
            author_links = article.find_elements(
                By.CSS_SELECTOR, ", ".join(self.AUTHOR_LINK_SELECTORS)
            )
            has_caption = bool(
                article.find_elements(
                    By.CSS_SELECTOR, '[data-ad-preview="message"]'
                )
            )
        except Exception:
            return False
        return bool(author_links) and not has_caption

    def _parse_comment(self, wrapper) -> dict:
        """Extract one comment dict from a wrapper element.  Never raises.

        All schema keys from PRD §7 are always present; unknown fields are None.
        """
        item = {
            "comment_id": None,
            "author_name": None,
            "author_profile_url": None,
            "comment_text": None,
            "comment_time": None,
            "comment_time_raw": None,
            "like_count": None,
            "reply_count": None,
            "is_reply": False,
            "parent_comment_id": None,
        }
        try:
            item["comment_id"] = self._extract_comment_id(wrapper)
        except Exception:
            pass
        # Modern FB: the author link carries a base64 "comment_id" query param
        # decoding to "comment:<a>_<b>".  For replies <a> is the parent
        # comment id and <b> the reply id; for top-level comments the id is
        # <b> (or <a>).  Some params do not decode -> fields stay null.
        try:
            id_links = wrapper.find_elements(
                By.CSS_SELECTOR, 'a[href*="comment_id="]'
            )
            href = id_links[0].get_attribute("href") if id_links else None
        except Exception:
            href = None
        try:
            comment_a, comment_b = self._decode_fb_comment_id(href)
        except Exception:
            comment_a, comment_b = None, None
        try:
            aria_label = (wrapper.get_attribute("aria-label") or "").lower()
        except Exception:
            aria_label = ""
        if comment_a or comment_b:
            if aria_label.startswith("reply by"):
                item["is_reply"] = True
                item["parent_comment_id"] = self._extract_parent_comment_id(wrapper)
                item["comment_id"] = comment_b or comment_a
            else:
                item["comment_id"] = comment_b or comment_a
        elif aria_label.startswith("reply by"):
            item["is_reply"] = True
            item["parent_comment_id"] = self._extract_parent_comment_id(wrapper)
        try:
            name, url = self._extract_author(wrapper)
            item["author_name"] = name
            item["author_profile_url"] = url
        except Exception:
            pass
        try:
            item["comment_text"] = self._extract_comment_text(
                wrapper, item["author_name"]
            )
        except Exception:
            pass
        try:
            time_val, time_raw = self._extract_comment_time(wrapper)
            item["comment_time"] = time_val
            item["comment_time_raw"] = time_raw
        except Exception:
            pass
        try:
            item["like_count"] = self._extract_like_count(wrapper)
        except Exception:
            pass
        try:
            item["reply_count"] = self._extract_reply_count(wrapper)
        except Exception:
            pass
        try:
            # OR-combine: the aria-label-based "Reply by" detection above may
            # already have set is_reply=True; never overwrite it with False.
            item["is_reply"] = item["is_reply"] or self._is_reply(wrapper)
            if item["is_reply"] and not item["parent_comment_id"]:
                item["parent_comment_id"] = self._extract_parent_comment_id(wrapper)
        except Exception:
            pass
        return item

    def _extract_comment_id(self, wrapper):
        """comment_id from data-commentid / data-ft (best effort)."""
        try:
            cid = wrapper.get_attribute("data-commentid")
        except Exception:
            cid = None
        if not cid:
            try:
                data_ft = wrapper.get_attribute("data-ft")
            except Exception:
                data_ft = None
            if data_ft:
                try:
                    payload = json.loads(data_ft)
                    for key in ("comment_id", "content_owner_id_new"):
                        val = payload.get(key)
                        if val not in (None, ""):
                            cid = str(val)
                            break
                except Exception:
                    cid = None
        if not cid:
            try:
                sub = wrapper.find_element(By.CSS_SELECTOR, "[data-commentid]")
                cid = sub.get_attribute("data-commentid")
            except Exception:
                cid = None
        return cid or None

    @staticmethod
    def _decode_fb_comment_id(href):
        """Decode FB's base64 ``comment_id`` query param from a comment link.

        The param decodes to ``comment:<a>_<b>`` (a reply, <a> = thread/parent
        id, <b> = reply id) or ``comment:<a>`` (a top-level comment).  Returns
        ``(a, b)`` as strings, either may be None.  Null-safe: never raises and
        returns ``(None, None)`` when the param format differs / fails to
        decode (some FB comments expose a different format).
        """
        if not href:
            return None, None
        try:
            parsed = urlparse(href)
            params = parse_qs(parsed.query)
            raw = params.get("comment_id")
            if not raw or not raw[0]:
                return None, None
            # parse_qs turns base64 "+" into a space; restore it.
            token = unquote(raw[0]).strip().replace(" ", "+")
        except Exception:
            return None, None

        decoded = None
        for candidate in (token + "==", token, token.rstrip("=") + "="):
            try:
                decoded = base64.b64decode(candidate).decode("utf-8", "ignore")
                break
            except Exception:
                continue
        if not decoded:
            return None, None

        m = FbScraper.REPLY_COMMENT_ID_RE.search(decoded)
        if not m:
            return None, None
        return m.group(1), m.group(2)

    def _extract_parent_comment_id(self, wrapper) -> Optional[str]:
        """parent_comment_id dari struktur DOM (thread container).

        Modern FB mengelompokkan setiap thread dalam container div yang berisi
        wrapper komentar top-level induk (``div[aria-label^="Comment by" i]``)
        sebagai descendant pertamanya; reply thread itu berada di container
        yang sama (reply-to-reply di-subgroup yang lebih dalam). Container
        dicari via descendant (``.//``) agar reply berkedalaman berapa pun
        tetap tertaut — untuk nested reply, fallback-nya adalah thread root.

        Decode base64 ``comment:<a>_<b>`` TIDAK bisa dipakai untuk parent:
        ``<a>`` adalah thread id yang DIBAGI semua komentar (top-level maupun
        reply), bukan id komentar induk; ``<b>`` adalah id milik komentar itu
        sendiri.
        """
        try:
            container = wrapper.find_element(
                By.XPATH,
                "./ancestor::div[.//div[starts-with(translate(@aria-label,"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                "'comment by')]][1]",
            )
            parent = container.find_element(
                By.XPATH,
                ".//div[starts-with(translate(@aria-label,"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                "'comment by')][1]",
            )
            for link in parent.find_elements(By.CSS_SELECTOR, 'a[href*="comment_id="]'):
                comment_a, comment_b = self._decode_fb_comment_id(
                    link.get_attribute("href")
                )
                if comment_a or comment_b:
                    return comment_b or comment_a
        except Exception:
            pass
        return None

    def _extract_author(self, wrapper):
        """Return (author_name, author_profile_url), either may be None.

        Modern FB: the author name is the first ``span[dir="auto"]`` in the
        wrapper (the ``user.php``/``profile.php`` author links are gone, so
        ``author_profile_url`` stays None).  Legacy FB variants that still
        expose author links are handled first.
        """
        name = None
        url = None
        try:
            links = wrapper.find_elements(
                By.CSS_SELECTOR, ", ".join(self.AUTHOR_LINK_SELECTORS)
            )
            if links:
                link = links[0]
                name = (link.text or "").strip() or None
                url = link.get_attribute("href") or None
        except Exception:
            pass
        if not name:
            # Modern FB: the author name is the first short span[dir="auto"].
            # Skip any span whose text equals the comment body (the body is
            # also mirrored in a span and must never become the author name).
            try:
                body_texts = {
                    (el.text or "").strip()
                    for el in wrapper.find_elements(
                        By.CSS_SELECTOR, 'div[dir="auto"]'
                    )
                    if (el.text or "").strip()
                }
            except Exception:
                body_texts = set()
            try:
                spans = wrapper.find_elements(By.CSS_SELECTOR, 'span[dir="auto"]')
                for span in spans:
                    text = (span.text or "").strip()
                    if text and len(text) < 120 and text not in body_texts:
                        name = text
                        break
            except Exception:
                pass
        return name, url

    def _extract_comment_text(self, wrapper, author_name=None):
        """Best-effort comment body text.

        Preferred source: ``div[data-ad-comment-text]``; fallback to the first
        ``div[dir="auto"]`` (comment body) that is not the author's name, then
        to the second ``span[dir="auto"]`` (modern FB mirrors the body there),
        then to the full wrapper text.
        """
        try:
            els = wrapper.find_elements(By.CSS_SELECTOR, "div[data-ad-comment-text]")
            for el in els:
                text = (el.text or "").strip()
                if text:
                    return text
        except Exception:
            pass
        try:
            dirs = wrapper.find_elements(By.CSS_SELECTOR, 'div[dir="auto"]')
            for el in dirs:
                text = (el.text or "").strip()
                if text:
                    if author_name and text == author_name:
                        continue
                    return text
        except Exception:
            pass
        # Modern FB also mirrors the body text in a second span[dir="auto"]
        # (the first span is the author name).
        try:
            spans = wrapper.find_elements(By.CSS_SELECTOR, 'span[dir="auto"]')
            for idx, span in enumerate(spans):
                text = (span.text or "").strip()
                if not text:
                    continue
                if author_name and text == author_name:
                    continue
                # Without a known author, treat the first short span as the
                # author name so it never becomes the comment body.
                if idx == 0 and not author_name and len(text) < 120:
                    continue
                return text
        except Exception:
            pass
        try:
            full = (wrapper.text or "").strip()
            if author_name and full.startswith(author_name):
                full = full[len(author_name):].lstrip()
            if full:
                return full
        except Exception:
            pass
        return None

    def _extract_comment_time(self, wrapper):
        """Return (iso_time, raw_text); either may be None."""
        try:
            abs_el = wrapper.find_element(By.CSS_SELECTOR, "abbr[data-utime]")
            ts = int(abs_el.get_attribute("data-utime"))
            if ts > 0:
                iso = (
                    datetime.fromtimestamp(ts)
                    .astimezone()
                    .isoformat(timespec="seconds")
                )
                return iso, None
        except NoSuchElementException:
            pass
        except Exception:
            pass
        try:
            abbr = wrapper.find_element(By.CSS_SELECTOR, "abbr[title]")
            title = (abbr.get_attribute("title") or "").strip()
            if title:
                return None, title
        except Exception:
            pass
        # Modern FB: the comment time is a link whose text is a relative time
        # like "3w", "2j", "5 hari" (no absolute abbr is present).
        try:
            time_links = wrapper.find_elements(By.CSS_SELECTOR, "a")
            for link in time_links:
                text = (link.text or "").strip()
                if not text or len(text) > 20:
                    continue
                if self.RELATIVE_TIME_RE.fullmatch(text):
                    return None, text
        except Exception:
            pass
        # Relative time text like "2j", "3 hari", "1h" (stored as raw).
        try:
            text = wrapper.text or ""
            m = self.RELATIVE_TIME_RE.search(text)
            if m:
                return None, m.group(1).strip()
        except Exception:
            pass
        return None, None

    def _extract_like_count(self, wrapper):
        for selector in (
            'div[data-testid="UFICommentLikeCount"]',
            'span[data-testid="UFICommentLikeCount"]',
        ):
            try:
                els = wrapper.find_elements(By.CSS_SELECTOR, selector)
                if els:
                    text = (els[0].text or "").strip()
                    if text.isdigit():
                        return int(text)
            except Exception:
                continue
        # Text fallback: "12 suka", "12 likes".
        try:
            m = self.LIKE_COUNT_RE.search(wrapper.text or "")
            if m:
                return int(m.group(1).replace(".", "").replace(",", ""))
        except Exception:
            pass
        # Modern FB: the like count is the trailing numeric token in the
        # wrapper text (e.g. "Toar Moningka\n<body>\n3w\n7"), as long as it is
        # a plausible small number.  MULTILINE $ keeps time tokens like "3w"
        # from being mistaken for a count.
        try:
            text = wrapper.text or ""
            matches = re.findall(r"(\d{1,5})\s*$", text, re.MULTILINE)
            if matches:
                val = int(matches[-1])
                if 1 <= val <= 99999:
                    return val
        except Exception:
            pass
        return None

    def _extract_reply_count(self, wrapper):
        try:
            m = self.REPLY_COUNT_RE.search(wrapper.text or "")
            if m:
                return int(m.group(1).replace(".", "").replace(",", ""))
        except Exception:
            pass
        return None

    def _is_reply(self, wrapper) -> bool:
        """Detect nested reply containers (UFIReply classes / nested articles).

        Modern FB nests *all* comments inside the post's ``div[role="article"]``,
        so an ancestor article only counts as a reply indicator when that
        ancestor is itself a comment/reply wrapper (``aria-label`` starting
        with "Comment by" / "Reply by"), not the post container.
        """
        try:
            cls = wrapper.get_attribute("class") or ""
            if "UFIReply" in cls:
                return True
        except Exception:
            pass
        # Nested inside another comment/reply article -> reply.
        try:
            if (wrapper.tag_name.lower() == "div"
                    and (wrapper.get_attribute("role") or "") == "article"):
                try:
                    parent_article = wrapper.find_element(
                        By.XPATH, "./ancestor::div[@role='article'][1]"
                    )
                except NoSuchElementException:
                    parent_article = None
                except Exception:
                    parent_article = None
                if parent_article is not None:
                    try:
                        aria = parent_article.get_attribute("aria-label") or ""
                    except Exception:
                        aria = ""
                    if aria.lower().startswith(("comment by", "reply by")):
                        return True
        except Exception:
            pass
        # Inside a reply list container.
        try:
            wrapper.find_element(
                By.XPATH,
                "./ancestor::div[contains(@class,'UFIReplyList') or "
                "contains(@class,'UFIRepliesList')][1]",
            )
            return True
        except NoSuchElementException:
            pass
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------ #
    # Expand-button discovery
    # ------------------------------------------------------------------ #
    def _find_more_comments_button(self):
        """Find the 'view more comments' control, or None if not present."""
        # 1) Attribute / data-testid based selectors.
        for selector in self.MORE_BUTTON_SELECTORS:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for el in els:
                if self._is_displayed(el):
                    return self._closest_clickable(el)
        # 2) Text based (English + Indonesian labels).
        best = None
        best_score = (-1, 0)

        def score(el, text):
            tag = el.tag_name.lower()
            role = ""
            try:
                role = el.get_attribute("role") or ""
            except Exception:
                role = ""
            clickable = 1 if (tag == "a" or (tag == "div" and role == "button")) else 0
            return (clickable, len(text))

        for selector in ("a[role='link']", "a", 'div[role="button"]', 'span[dir="auto"]'):
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                continue
            for el in els:
                try:
                    text = (el.text or "").strip()
                except Exception:
                    continue
                if not text or len(text) > 200:
                    continue
                if self.MORE_BUTTON_TEXT_RE.search(text):
                    if not self._is_displayed(el):
                        continue
                    s = score(el, text)
                    if s > best_score:
                        best_score = s
                        best = el
        if best is not None:
            return self._closest_clickable(best)
        return None

    def _closest_clickable(self, el):
        """Walk up from a matched element to the closest clickable ancestor."""
        cur = el
        for _ in range(4):
            try:
                tag = cur.tag_name.lower()
                role = cur.get_attribute("role") or ""
            except Exception:
                return cur
            if tag == "a" or (tag == "div" and role == "button"):
                return cur
            try:
                parent = self.driver.execute_script(
                    "return arguments[0].parentNode;", cur
                )
            except Exception:
                return cur
            if parent is None or parent == cur:
                return cur
            cur = parent
        return cur

    # ------------------------------------------------------------------ #
    # Post caption (best effort)
    # ------------------------------------------------------------------ #
    def _extract_post_caption(self):
        """Best-effort post caption text; None when not found."""
        with implicit_wait_off(self.driver):
            for selector in self.CAPTION_SELECTORS:
                try:
                    els = self.driver.find_elements(By.CSS_SELECTOR, selector)
                except Exception:
                    continue
                for el in els:
                    try:
                        text = (el.text or "").strip()
                    except Exception:
                        text = ""
                    if text:
                        return text
            # Fallback: long span[dir="auto"] text (post body).
            try:
                spans = self.driver.find_elements(By.CSS_SELECTOR, 'span[dir="auto"]')
                best = None
                for s in spans:
                    try:
                        text = (s.text or "").strip()
                    except Exception:
                        continue
                    if len(text) >= 40:
                        if best is None or len(text) > len(best):
                            best = text
                if best:
                    return best
            except Exception:
                pass
            # Fallback: the first div[dir="auto"] inside the post article.
            try:
                articles = self.driver.find_elements(
                    By.CSS_SELECTOR, 'div[role="article"]'
                )
                if articles:
                    dirs = articles[0].find_elements(
                        By.CSS_SELECTOR, 'div[dir="auto"]'
                    )
                    if dirs:
                        text = (dirs[0].text or "").strip()
                        if text:
                            return text
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------ #
    # Small helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_displayed(el) -> bool:
        try:
            return el.is_displayed()
        except Exception:
            return False
