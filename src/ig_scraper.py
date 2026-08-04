"""Instagram public post comment scraper.

Modul ini hanya bertanggung jawab untuk *satu* halaman postingan Instagram
publik: membuka URL, memeriksa login-wall & rate-limit, memuat komentar
(lazy-load via scroll), mengekstrak data komentar, dan mengembalikan dict
sesuai skema PRD §7. Penulisan file JSON dilakukan oleh `main.py`.

Dokumentasi selector (acuan PRD §13 Rencana Pengujian Manual)
--------------------------------------------------------------
* Caption postingan (best-effort, None jika tidak ditemukan):
    1. `div[role="button"] h1`            -> caption di area mirip komentar
    2. `ul > div > li:first-child`        -> item pertama pada daftar komentar
    3. `h1` + `span[dir="auto"]`          -> h1 dan span yang mengikutinya
* Container komentar (elemen yang di-scroll untuk memuat komentar lebih banyak):
    1. `div[role="dialog"] ul`
    2. `div[role="dialog"] div[style*="max-height"]`
    3. `div[role="dialog"] div[style*="overflow-y"]` (scrolling comment section)
    4. `div[role="dialog"] div[style*="overflow"]`
    5. `main ul` (layout single-column tanpa dialog)
    Fallback: scroll seluruh halaman (lihat `_human_scroll`). Catatan:
    `human_scroll` bukan bagian dari kontrak modul bersama `src.human_behavior`
    (kontrak hanya sleep_random, human_move_to, human_click, scroll_container,
    look_around), sehingga fallback scroll halaman diimplementasikan lokal di
    sini agar tidak bergantung pada simbol yang tidak dijamin ada.
* Item komentar:
    - Prefer: `<li>` di dalam container / `div[role="dialog"] ul li`
    - Fallback: `div[role="presentation"]` di dalam dialog (baris komentar baru)
    Field per item:
      comment_id         : attribute `id` / `data-id` pada item
      author_name        : `h3`, atau teks link `a[href^="/"]`
      author_profile_url : href dari `a[href^="/"]` (di-prefix domain bila relatif)
      comment_text       : teks `span[dir="auto"]` / `div[dir="auto"]` pada item
                           (nama author di-exclude)
      comment_time       : `<time datetime=...>` absolut -> ISO 8601 tz lokal;
                           relatif ("2h", "3 hari") -> null + field comment_time_raw
      like_count         : `[aria-label*="likes"]` / angka dekat ikon hati
      reply_count        : teks "View replies (N)" / "Balas (N)" / "N replies"
      is_reply           : struktur bersarang (`ancestor::ul[2]` ada) atau teks
                           diawali mention "@..." (default False)
      parent_comment_id  : null

Catatan: ekstraksi teks komentar IG bersifat rapuh; setiap item dibungkus
try/except sehingga satu item yang buruk tidak menghentikan seluruh proses.
"""

from __future__ import annotations

import datetime
import random
import re
from typing import Optional, Tuple

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from src.exceptions import LoginRequiredError, RateLimitError, ScrapeError
from src.browser_utils import implicit_wait_off
from src.human_behavior import (
    human_click,
    human_move_to,
    look_around,
    scroll_container,
    sleep_random,
)
from src.io_utils import extract_post_id
from src.login_wall_detector import (
    detect_login_wall,
    detect_rate_limit,
    dismiss_login_popup,
)
from src.logger import get_logger

logger = get_logger(__name__)


class IgScraper:
    """Scraper komentar untuk postingan Instagram publik."""

    PLATFORM = "IG"

    MAX_SCROLL_ITERATIONS = 30
    STABLE_ROUNDS_LIMIT = 3

    # Selector container komentar (prioritas: dialog, lalu main).
    COMMENT_CONTAINER_SELECTORS = [
        'div[role="dialog"] ul',
        'div[role="dialog"] div[style*="max-height"]',
        'div[role="dialog"] div[style*="overflow-y"]',
        'div[role="dialog"] div[style*="overflow"]',
        "main ul",
    ]

    VIEW_ALL_COMMENTS_PATTERN = re.compile(
        r"view all\s*([\d.,]*)\s*comments", re.I
    )
    LOAD_MORE_COMMENTS_PATTERN = re.compile(r"load more comments", re.I)
    REPLY_COUNT_PATTERN = re.compile(
        r"(?:view replies|lihat balasan|balas)\s*\(?([\d.,]+)\)?", re.I
    )
    REPLY_COUNT_ALT_PATTERN = re.compile(
        r"([\d.,]+)\s*(?:replies|balasan)", re.I
    )
    LIKE_LABEL_PATTERN = re.compile(
        r"([\d.,]+)\s*(?:likes|like|suka)", re.I
    )
    MENTION_PREFIX_PATTERN = re.compile(r"^@[\w._]+")

    def __init__(self, driver: WebDriver, config: dict):
        self.driver = driver
        self.config = config
        self.logger = get_logger(f"{__name__}.IgScraper")

    # ------------------------------------------------------------- public API

    def scrape_post(self, url: str) -> dict:
        """Scrape komentar publik satu postingan Instagram.

        Raises:
            LoginRequiredError: postingan membutuhkan login / tidak publik.
            RateLimitError: pola rate-limit terdeteksi (hentikan batch).
            ScrapeError: error fatal tingkat halaman.
        """
        try:
            self._open_post(url)
            self._check_login_wall()
            self._check_rate_limit()
            caption = self._extract_caption()
            items = self._load_comments()
            max_comments = self._max_comments()
            comments = self._collect_comments(items, max_comments, caption)

            result = {
                "post_id": extract_post_id(url, "ig"),
                "platform": self.PLATFORM,
                "post_url": url,
                "scraped_at": datetime.datetime.now().astimezone().isoformat(),
                "post_caption": caption,
                "total_comments_scraped": len(comments),
                "comments": comments,
            }
            self.logger.info(
                "[IG] Selesai: %d komentar dari %s", len(comments), url
            )
            return result
        except (LoginRequiredError, RateLimitError, ScrapeError):
            raise
        except Exception as exc:
            raise ScrapeError(f"Gagal scrape Instagram {url}: {exc}") from exc

    # -------------------------------------------------------------- setup/cek

    def _open_post(self, url: str) -> None:
        try:
            self.driver.get(url)
        except Exception as exc:
            raise ScrapeError(f"Gagal memuat halaman {url}: {exc}") from exc
        sleep_random(self._sec_range("between_actions_sec"))

    def _check_login_wall(self) -> None:
        # Dismiss any dismissible login/nag popup first — IG overlays a
        # "Log in to see" popup on content that may be public; closing it
        # reveals the post. Real login walls are still caught below.
        if dismiss_login_popup(self.driver, "ig"):
            sleep_random((1, 2))
        timeout_sec = float(
            self.config.get("scraping", {}).get("comments_wait_sec", 15.0)
        )
        hit, reason = detect_login_wall(
            self.driver, "ig", timeout_sec=timeout_sec
        )
        if hit:
            self.logger.warning("[IG] Login-wall terdeteksi: %s", reason)
            raise LoginRequiredError(reason)

    def _check_rate_limit(self) -> None:
        hit, marker = detect_rate_limit(self.driver)
        if hit:
            self.logger.warning("[IG] Rate-limit terdeteksi: %s", marker)
            raise RateLimitError(marker)

    # --------------------------------------------------------------- caption

    def _extract_caption(self) -> Optional[str]:
        """Ekstrak caption postingan secara best-effort (None jika gagal)."""
        with implicit_wait_off(self.driver):
            # 1) Caption di area mirip komentar.
            try:
                el = self.driver.find_element(
                    By.CSS_SELECTOR, 'div[role="button"] h1'
                )
                text = self._clean_text(el.text)
                if text:
                    return text
            except Exception:
                pass
            # 2) Item pertama di daftar komentar (caption sering menjadi item #1).
            try:
                el = self.driver.find_element(
                    By.CSS_SELECTOR, "ul > div > li:first-child"
                )
                text = self._clean_text(el.text)
                if text:
                    return text
            except Exception:
                pass
            # 3) h1 + span yang mengikutinya.
            try:
                h1 = self.driver.find_element(By.TAG_NAME, "h1")
                h1_text = self._clean_text(h1.text)
                if h1_text:
                    extras = []
                    parent = h1.find_element(By.XPATH, "..")
                    for span in parent.find_elements(
                        By.CSS_SELECTOR, "span[dir='auto']"
                    ):
                        t = self._clean_text(span.text)
                        if t and t != h1_text and t not in extras:
                            extras.append(t)
                    combined = " ".join([h1_text] + extras).strip()
                    if combined:
                        return combined
            except Exception:
                pass
        return None

    # -------------------------------------------------------- loading komentar

    def _max_comments(self) -> int:
        try:
            return int(self.config["scraping"]["max_comments_per_post"])
        except Exception:
            return 500

    def _sec_range(self, key: str = "between_actions_sec") -> Tuple[float, float]:
        try:
            r = self.config["delays"][key]
            return (float(r[0]), float(r[1]))
        except Exception:
            return (2.0, 6.0)

    def _scroll_step_range(self) -> Tuple[int, int]:
        try:
            r = self.config["delays"]["scroll_step_px"]
            return (int(r[0]), int(r[1]))
        except Exception:
            return (300, 800)

    def _look_around_probability(self) -> float:
        try:
            return float(self.config["delays"]["look_around_probability"])
        except Exception:
            return 0.3

    def _find_comment_container(self) -> Optional[WebElement]:
        for css in self.COMMENT_CONTAINER_SELECTORS:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, css)
                if el.is_displayed():
                    return el
            except Exception:
                continue
        return None

    def _find_comment_items(self, container: Optional[WebElement]):
        """Kembalikan list item komentar (li, fallback div baris komentar)."""
        try:
            if container is not None:
                lis = container.find_elements(By.CSS_SELECTOR, "li")
                if lis:
                    return lis
            lis = self.driver.find_elements(
                By.CSS_SELECTOR, 'div[role="dialog"] ul li'
            )
            if lis:
                return lis
            lis = self.driver.find_elements(By.CSS_SELECTOR, "main ul li")
            if lis:
                return lis
            divs = self.driver.find_elements(
                By.CSS_SELECTOR, 'div[role="dialog"] div[role="presentation"]'
            )
            return divs
        except Exception:
            return []

    def _click_view_all_comments(self) -> None:
        """Klik tombol 'View all comments' agar dialog komentar terbuka penuh."""
        try:
            for el in self.driver.find_elements(
                By.CSS_SELECTOR,
                'div[role="button"], button, span[role="button"]',
            ):
                try:
                    if not el.is_displayed():
                        continue
                    txt = self._clean_text(el.text)
                    if txt and self.VIEW_ALL_COMMENTS_PATTERN.search(txt):
                        human_move_to(self.driver, el)
                        human_click(self.driver, el)
                        sleep_random(self._sec_range())
                        return
                except Exception:
                    continue
        except Exception:
            pass

    def _click_load_more(self) -> bool:
        """Klik 'Load more comments' bila ada (komplementer dengan scroll)."""
        try:
            for el in self.driver.find_elements(
                By.CSS_SELECTOR,
                'div[role="button"], button, span[role="button"]',
            ):
                try:
                    if not el.is_displayed():
                        continue
                    txt = self._clean_text(el.text)
                    if txt and self.LOAD_MORE_COMMENTS_PATTERN.search(txt):
                        human_move_to(self.driver, el)
                        human_click(self.driver, el)
                        sleep_random(self._sec_range())
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _human_scroll(self) -> None:
        """Fallback scroll seluruh halaman (lokal, lihat docstring modul)."""
        try:
            step = int(random.uniform(300, 700))
            self.driver.execute_script(
                "window.scrollBy({top: arguments[0], behavior: 'smooth'});",
                step,
            )
        except Exception:
            pass

    def _load_comments(self):
        """Muat komentar via scroll berulang sampai berhenti bertambah."""
        # Bulk DOM scans must not stall on the implicit wait; explicit
        # sleep_random pauses below give lazy-loaded content time to appear.
        with implicit_wait_off(self.driver):
            self._click_view_all_comments()
            max_comments = self._max_comments()
            last_count = -1
            stable_rounds = 0

            for iteration in range(self.MAX_SCROLL_ITERATIONS):
                container = self._find_comment_container()
                items = self._find_comment_items(container)
                count = len(items)

                if count > last_count:
                    stable_rounds = 0
                else:
                    stable_rounds += 1
                last_count = count

                self.logger.info(
                    "[IG] Iterasi %d/%d: %d item komentar terlihat",
                    iteration + 1,
                    self.MAX_SCROLL_ITERATIONS,
                    count,
                )

                if count >= max_comments:
                    self.logger.info("[IG] Batas max_comments tercapai (%d).", max_comments)
                    break
                if stable_rounds >= self.STABLE_ROUNDS_LIMIT:
                    self.logger.info("[IG] Jumlah komentar tidak bertambah, berhenti memuat.")
                    break

                try:
                    clicked = self._click_load_more()
                except Exception:
                    clicked = False

                if not clicked:
                    try:
                        if container is not None:
                            scroll_container(
                                self.driver,
                                container,
                                step_px_range=self._scroll_step_range(),
                            )
                        else:
                            self._human_scroll()
                    except Exception as exc:
                        self.logger.debug("[IG] Gagal scroll container: %s", exc)
                        self._human_scroll()

                sleep_random(self._sec_range())

                if random.random() < self._look_around_probability():
                    try:
                        look_around(self.driver)
                    except Exception as exc:
                        self.logger.debug("[IG] look_around gagal: %s", exc)

                self._check_rate_limit()

            return self._find_comment_items(self._find_comment_container())

    # ---------------------------------------------------------- ekstraksi item

    def _collect_comments(self, items, max_comments: int, caption: Optional[str]):
        comments = []
        seen = set()
        # Per-item extraction does many find_elements calls; keep them fast.
        with implicit_wait_off(self.driver):
            for item in items:
                if len(comments) >= max_comments:
                    break
                try:
                    if self._looks_like_caption(item, caption):
                        continue
                    comment = self._build_comment(item)
                    if comment is None:
                        continue
                    if not comment.get("author_name") and not comment.get("comment_text"):
                        # Item kosong/junk, kemungkinan bukan komentar.
                        continue
                    key = self._comment_key(comment)
                    if key in seen:
                        continue
                    seen.add(key)
                    comments.append(comment)
                except Exception as exc:
                    self.logger.debug("[IG] Item komentar di-skip: %s", exc)
                    continue
        return comments

    @staticmethod
    def _comment_key(comment: dict):
        if comment.get("comment_id"):
            return ("id", comment["comment_id"])
        return ("text", comment.get("author_name"), comment.get("comment_text"))

    @staticmethod
    def _looks_like_caption(item: WebElement, caption: Optional[str]) -> bool:
        if not caption:
            return False
        try:
            item_text = IgScraper._clean_text(item.text)
            return bool(item_text) and item_text == IgScraper._clean_text(caption)
        except Exception:
            return False

    def _build_comment(self, item: WebElement) -> dict:
        author_name, author_url = self._extract_author(item)
        comment_text = self._extract_comment_text(item, author_name)
        comment_time, time_raw = self._extract_time(item)
        like_count = self._extract_like_count(item)
        reply_count = self._extract_reply_count(item)
        is_reply = self._is_reply(item)

        comment = {
            "comment_id": self._extract_comment_id(item),
            "author_name": author_name,
            "author_profile_url": author_url,
            "comment_text": comment_text,
            "comment_time": comment_time,
            "like_count": like_count,
            "reply_count": reply_count,
            "is_reply": is_reply,
            "parent_comment_id": None,
        }
        if time_raw:
            comment["comment_time_raw"] = time_raw
        return comment

    @staticmethod
    def _extract_comment_id(item: WebElement) -> Optional[str]:
        for attr in ("id", "data-id"):
            try:
                val = item.get_attribute(attr)
                if val:
                    return val
            except Exception:
                continue
        return None

    def _extract_author(self, item: WebElement):
        name = None
        url = None
        # Nama + URL dari link profil author (href dimulai "/").
        try:
            link = item.find_element(By.CSS_SELECTOR, 'a[href^="/"]')
            href = link.get_attribute("href")
            if href:
                if href.startswith("http"):
                    url = href
                else:
                    url = "https://www.instagram.com" + href
            link_text = self._clean_text(link.text)
            if link_text:
                name = link_text.lstrip("@").strip()
        except Exception:
            pass
        # Fallback nama dari h3.
        if not name:
            try:
                h3 = item.find_element(By.CSS_SELECTOR, "h3")
                name = self._clean_text(h3.text).lstrip("@").strip()
            except Exception:
                pass
        return (name or None), (url or None)

    def _extract_comment_text(
        self, item: WebElement, author_name: Optional[str]
    ) -> Optional[str]:
        try:
            parts = []
            for el in item.find_elements(
                By.CSS_SELECTOR, 'span[dir="auto"], div[dir="auto"]'
            ):
                t = self._clean_text(el.text)
                if t and t not in parts:
                    parts.append(t)
            text = " ".join(parts) if parts else self._clean_text(item.text)
            # Buang nama author bila ikut terambil.
            if author_name and text.startswith(author_name):
                text = text[len(author_name):].strip()
            # Buang prefix mention "@username" pada komentar balasan.
            text = self.MENTION_PREFIX_PATTERN.sub("", text).strip()
            return text or None
        except Exception:
            return None

    def _extract_time(self, item: WebElement):
        """Kembalikan (comment_time, comment_time_raw)."""
        raw = None
        try:
            t = item.find_element(By.CSS_SELECTOR, "time")
            raw = self._clean_text(t.text) or None
            dt_attr = t.get_attribute("datetime")
            if dt_attr:
                iso = self._parse_datetime(dt_attr)
                if iso:
                    return iso, None
            return None, raw
        except Exception:
            return None, raw

    @staticmethod
    def _parse_datetime(value: str) -> Optional[str]:
        """Parse datetime attribute ke ISO 8601 dengan offset tz lokal."""
        try:
            s = value.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone().isoformat()
        except Exception:
            return None

    def _extract_like_count(self, item: WebElement) -> Optional[int]:
        # 1) aria-label mengandung "N likes"/"N suka".
        try:
            for el in item.find_elements(By.CSS_SELECTOR, "[aria-label]"):
                label = el.get_attribute("aria-label") or ""
                m = self.LIKE_LABEL_PATTERN.search(label)
                if m:
                    return self._to_int(m.group(1))
        except Exception:
            pass
        # 2) Angka di dekat ikon hati (best-effort).
        try:
            for svg in item.find_elements(
                By.CSS_SELECTOR,
                'svg[aria-label="Like"], svg[aria-label="Suka"]',
            ):
                box = svg.find_element(By.XPATH, "..")
                for span in box.find_elements(By.CSS_SELECTOR, "span"):
                    t = self._clean_text(span.text)
                    if t and re.fullmatch(r"[\d.,]+", t):
                        return self._to_int(t)
        except Exception:
            pass
        return None

    def _extract_reply_count(self, item: WebElement) -> Optional[int]:
        try:
            text = self._clean_text(item.text)
            m = self.REPLY_COUNT_PATTERN.search(text)
            if m:
                return self._to_int(m.group(1))
            m = self.REPLY_COUNT_ALT_PATTERN.search(text)
            if m:
                return self._to_int(m.group(1))
        except Exception:
            pass
        return None

    def _is_reply(self, item: WebElement) -> bool:
        # Komentar balasan hidup dalam list bersarang (ul di dalam ul).
        try:
            if item.find_elements(By.XPATH, "./ancestor::ul[2]"):
                return True
        except Exception:
            pass
        # Fallback: teks komentar balasan biasanya diawali mention "@...".
        try:
            text = self._clean_text(item.text)
            if self.MENTION_PREFIX_PATTERN.match(text):
                return True
        except Exception:
            pass
        return False

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _clean_text(text) -> str:
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _to_int(value: Optional[str]) -> Optional[int]:
        try:
            return int(re.sub(r"[^\d]", "", value or ""))
        except Exception:
            return None
