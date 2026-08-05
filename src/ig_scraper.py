"""Instagram public post comment scraper.

Modul ini hanya bertanggung jawab untuk *satu* halaman postingan Instagram
publik: membuka URL, memeriksa login-wall & rate-limit, memuat komentar
(load-more via tombol "Load more comments"), mengekstrak data komentar, dan
mengembalikan dict sesuai skema PRD §7. Penulisan file JSON dilakukan oleh
`main.py`.

Dokumentasi selector DOM Instagram saat ini (2025+)
----------------------------------------------------
Instagram me-render komentar LANGSUNG di halaman postingan (single-column
layout) — tidak lagi memakai dialog komentar (`div[role="dialog"]`) dengan
`ul/li`. Struktur yang relevan:

* Container komentar (terlihat di halaman postingan):
    `div.x9f619.x78zum5.xdt5ytf.x5yr21d.xexx8yu.xv54qhq.x1l90r2v.xf7dkkf.x10l6tqk.xh8yej3`
    - Child langsung pertama : blok owner + caption (baris pertama = username
      pemilik postingan, sisanya = caption).
    - Child langsung ketiga  : daftar komentar
      (`div.x78zum5.xdt5ytf.x1iyjqo2`).
* Baris komentar: child langsung dari daftar komentar; class dimulai
  `html-div xdj266r ...`. Teks baris berbentuk:
      "<author>\n <time>\n<comment_text>\nLike\nReply"
  atau, bila ada like:
      "<author>\n <time>\n<comment_text>\n<N> like\nReply"
* Author  : link `a[href="https://www.instagram.com/<username>/"]` pertama
  pada baris (bukan link `/p/...` dan bukan link `/c/...`); `.text`-nya
  adalah nama author.
* comment_id: link `a[href="https://www.instagram.com/p/<post>/c/<numeric>/"]`
  -> segmen numerik setelah `/c/` (contoh:
  `https://www.instagram.com/p/DaFE3sZvBHx/c/18110673211979640/` ->
  `18110673211979640`). Catatan: `.text` link ini adalah waktu relatif
  (mis. "1w"), bukan teks komentar.
* Waktu komentar: elemen `time` (atau `span`) dengan teks waktu relatif
  ("1w", "4w", "5h", "2d", "2 hari") -> disimpan sebagai `comment_time_raw`.
* Teks komentar: `span` yang mengikuti elemen waktu; fallback dari baris teks
  (baris antara author/waktu dan Like/Reply).
* like_count: baris `^([\\d.,]+)\\s+like$` pada teks baris; "Like" tanpa angka
  -> None.
* is_reply: default False; True hanya bila teks baris memuat penanda
  "View all N replies" / "view replies" (replies ter-collapse di layout
  single-column; `parent_comment_id` selalu null).
* Memuat komentar tambahan: SATU-SATUNYA mekanisme adalah meng-scroll elemen
  yang benar-benar scrollable — PARENT container komentar
  (`div.x5yr21d.xw2csxc.x1odjw0f.x1n2onr6`, overflow-y: auto); container
  komentar itu sendiri (`div.x9f619...xh8yej3`) overflow: visible dan TIDAK
  bisa discroll. Lazy-load IG terpicu saat PARENT digulir bertahap lalu
  didorong ke dasar berulang kali (scrollTop = scrollHeight; tiap dorongan
  memicu satu batch lazy-load). Scroll window TIDAK berfungsi. Klik tombol
  `svg[aria-label="Load more comments"]` TIDAK dipakai lagi: klik memunculkan
  popup login dan `remove_login_popup_and_overlay` mereset state lazy-load,
  menghentikan pertumbuhan komentar selanjutnya (terverifikasi lewat A/B test
  live).

Catatan: ekstraksi teks komentar IG bersifat rapuh; setiap baris komentar
dibungkus try/except sehingga satu baris yang buruk tidak menghentikan
seluruh proses.
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
from src.human_behavior import look_around, sleep_random
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

    # Selector container komentar (DOM Instagram saat ini, single-column).
    COMMENT_CONTAINER_SELECTOR = (
        "div.x9f619.x78zum5.xdt5ytf.x5yr21d.xexx8yu.xv54qhq.x1l90r2v"
        ".xf7dkkf.x10l6tqk.xh8yej3"
    )
    # Elemen yang SEBENARNYA scrollable (overflow-y: auto): parent dari
    # container komentar. Container x9f619... sendiri overflow: visible dan
    # tidak bisa discroll; lazy-load IG terpicu saat PARENT ini digulir.
    COMMENT_SCROLL_CONTAINER_SELECTOR = "div.x5yr21d.xw2csxc.x1odjw0f.x1n2onr6"
    # Child langsung ke-3 dari container = daftar komentar; setiap child
    # langsungnya adalah satu baris komentar.
    COMMENT_LIST_SELECTOR = "div.x78zum5.xdt5ytf.x1iyjqo2"
    COMMENT_LIST_MARKER = "x1iyjqo2"

    # Link komentar: https://www.instagram.com/p/<post>/c/<numeric>/
    COMMENT_ID_RE = re.compile(r"/c/(\d+)")
    RELATIVE_TIME_RE = re.compile(
        r"(?i)(\d+\s*(detik|menit|jam|hari|minggu|bulan|tahun|[smhdwj]))"
    )
    LIKE_LINE_RE = re.compile(r"^([\d.,]+)\s+like$", re.I)
    VIEW_REPLIES_RE = re.compile(r"(?i)view\s*(?:all\s+[\d.,]+\s+)?replies")

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
        """Ekstrak caption postingan secara best-effort (None jika gagal).

        Pada DOM baru, blok owner+caption adalah child langsung pertama dari
        container komentar: baris pertama = username pemilik, sisanya = caption.
        """
        with implicit_wait_off(self.driver):
            try:
                container = self._find_comment_container()
            except Exception:
                container = None
            if container is None:
                return None

            # 1) Child langsung pertama = blok owner + caption.
            try:
                children = container.find_elements(By.XPATH, "./div")
                if children:
                    lines = [
                        l.strip()
                        for l in (children[0].text or "").split("\n")
                        if l.strip()
                    ]
                    if len(lines) > 1:
                        caption = " ".join(lines[1:]).strip()
                        if caption:
                            return caption
            except Exception:
                pass

            # 2) Fallback: baris container.text sebelum baris komentar pertama
            #    (baris author komentar selalu diikuti baris waktu relatif).
            try:
                lines = [
                    l.strip()
                    for l in (container.text or "").split("\n")
                    if l.strip()
                ]
                if not lines:
                    return None
                parts = []
                for i in range(1, len(lines)):
                    line = lines[i]
                    following = lines[i + 1] if i + 1 < len(lines) else ""
                    if self.RELATIVE_TIME_RE.fullmatch(line):
                        break  # baris waktu komentar
                    if self.RELATIVE_TIME_RE.fullmatch(following):
                        break  # baris ini = author komentar (diikuti waktu)
                    if self._is_control_line(line):
                        break
                    parts.append(line)
                if parts:
                    caption = " ".join(parts).strip()
                    if caption:
                        return caption
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

    def _look_around_probability(self) -> float:
        try:
            return float(self.config["delays"]["look_around_probability"])
        except Exception:
            return 0.3

    def _find_comment_container(self) -> Optional[WebElement]:
        """Kembalikan elemen container komentar, atau None."""
        with implicit_wait_off(self.driver):
            # 1) Selector utama (class container saat ini).
            try:
                el = self.driver.find_element(
                    By.CSS_SELECTOR, self.COMMENT_CONTAINER_SELECTOR
                )
                if el.is_displayed():
                    return el
            except Exception:
                pass
            # 2) Defensive fallback bila class berubah: cari link komentar
            #    `/c/` di dalam main, lalu naik ke ancestor pembawa class
            #    layout container / daftar komentar.
            try:
                links = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    'main div[role="main"] a[href*="/c/"], main a[href*="/c/"]',
                )
                for link in links:
                    try:
                        container = link.find_element(
                            By.XPATH,
                            "./ancestor::div[contains(concat(' ', "
                            "normalize-space(@class), ' '), ' x9f619 ') or "
                            "contains(concat(' ', normalize-space(@class), ' '), "
                            "' x1iyjqo2 ')][1]",
                        )
                    except Exception:
                        container = None
                    if container is not None:
                        try:
                            if container.is_displayed():
                                return container
                        except Exception:
                            pass
            except Exception:
                pass
            return None

    def _find_scroll_container(self) -> Optional[WebElement]:
        """Kembalikan elemen yang SEBENARNYA scrollable untuk lazy-load komentar.

        Container komentar (`div.x9f619...xh8yej3`) memiliki overflow: visible
        dan tidak bisa digulir; elemen scrollable adalah PARENT-nya
        (`div.x5yr21d.xw2csxc.x1odjw0f.x1n2onr6`, overflow-y: auto). Lazy-load
        IG terpicu saat elemen inilah yang di-scroll.
        """
        with implicit_wait_off(self.driver):
            # 1) Selector utama (parent scrollable yang diketahui).
            try:
                el = self.driver.find_element(
                    By.CSS_SELECTOR, self.COMMENT_SCROLL_CONTAINER_SELECTOR
                )
                if el.is_displayed():
                    return el
            except Exception:
                pass
            # 2) Fallback: naik dari container komentar ke ancestor yang
            #    benar-benar scrollable (overflowY auto/scroll/overlay dengan
            #    scrollHeight > clientHeight). Selenium mengembalikan WebElement
            #    untuk node DOM yang dikembalikan execute_script.
            try:
                container = self._find_comment_container()
                if container is None:
                    return None
                el = self.driver.execute_script(
                    """
                    var el = arguments[0];
                    while (el) {
                        var oy = window.getComputedStyle(el).overflowY;
                        if (['auto', 'scroll', 'overlay'].indexOf(oy) !== -1
                                && el.scrollHeight > el.clientHeight) {
                            return el;
                        }
                        el = el.parentElement;
                    }
                    return null;
                    """,
                    container,
                )
                if el is not None:
                    return el
            except Exception:
                pass
            return None

    def _find_comment_rows(self, container: Optional[WebElement]):
        """Kembalikan list baris komentar (child langsung dari daftar komentar)."""
        rows = []
        seen_ids = set()

        def add(row):
            try:
                row_id = row.id
            except Exception:
                return
            if row_id in seen_ids:
                return
            seen_ids.add(row_id)
            rows.append(row)

        with implicit_wait_off(self.driver):
            list_el = None
            if container is not None:
                try:
                    cls = container.get_attribute("class") or ""
                except Exception:
                    cls = ""
                if self.COMMENT_LIST_MARKER in cls:
                    # Container fallback bisa jadi adalah daftar komentar itu sendiri.
                    list_el = container
                else:
                    # Daftar komentar = child langsung dengan class x1iyjqo2
                    # (child ke-3 container pada DOM saat ini).
                    try:
                        for child in container.find_elements(By.XPATH, "./div"):
                            try:
                                child_cls = child.get_attribute("class") or ""
                            except Exception:
                                child_cls = ""
                            if self.COMMENT_LIST_MARKER in child_cls:
                                list_el = child
                                break
                    except Exception:
                        list_el = None
                    if list_el is None:
                        try:
                            list_el = container.find_element(
                                By.CSS_SELECTOR, self.COMMENT_LIST_SELECTOR
                            )
                        except Exception:
                            list_el = None
                if list_el is not None:
                    try:
                        children = list_el.find_elements(By.XPATH, "./div")
                    except Exception:
                        children = []
                    for row in children:
                        try:
                            if not row.is_displayed():
                                continue
                        except Exception:
                            pass
                        add(row)

            # Fallback: scan div di dalam main yang teksnya menyerupai baris
            # komentar (diakhiri "Like\nReply" / "<N> like\nReply", pendek).
            if not rows:
                try:
                    divs = self.driver.find_elements(By.CSS_SELECTOR, "main div")
                except Exception:
                    divs = []
                for div in divs:
                    try:
                        text = div.text or ""
                        if not text or len(text) >= 400:
                            continue
                        tail = [
                            l.strip().lower()
                            for l in text.split("\n")
                            if l.strip()
                        ]
                        if len(tail) >= 2 and tail[-1] == "reply" and (
                            tail[-2] == "like"
                            or re.fullmatch(r"[\d.,]+\s+like", tail[-2])
                        ):
                            add(div)
                    except Exception:
                        continue
        return rows

    def _scroll_comment_container(self) -> None:
        """Scroll elemen scrollable untuk memicu lazy-load komentar IG.

        Container komentar (`div.x9f619...xh8yej3`) memiliki overflow: visible —
        setting scrollTop padanya tidak menghasilkan apa-apa. Elemen yang
        benar-benar scrollable adalah PARENT-nya
        (`div.x5yr21d.xw2csxc.x1odjw0f.x1n2onr6`, overflow-y: auto). Elemen ini
        TIDAK di-scrollIntoView: saat komentar lazy-load, elemen tumbuh jauh
        lebih tinggi dari viewport sehingga scrollIntoView justru menarik
        scrollTop ke TENGAH elemen dan mengganggu pemicu lazy-load IG.

        Satu dorongan ke dasar (scrollTop = scrollHeight) memicu SATU batch
        lazy-load; karena itu dilakukan dorongan berulang (3x per panggilan,
        jeda antar dorongan) agar jumlah komentar tumbuh stabil — sesuai
        eksperimen live yang terverifikasi.
        """
        try:
            with implicit_wait_off(self.driver):
                scroll_el = self._find_scroll_container()
                if scroll_el is None:
                    return
                # Langkah kecil alami terlebih dahulu, seperti manusia.
                for _ in range(random.randint(2, 4)):
                    step = int(random.uniform(250, 450))
                    self.driver.execute_script(
                        "arguments[0].scrollTop += arguments[1];",
                        scroll_el,
                        step,
                    )
                    sleep_random((0.3, 0.7))
                # Dorongan berulang ke dasar: tiap dorongan memicu satu batch
                # lazy-load, sehingga perlu diulang agar pertumbuhan stabil.
                for _ in range(3):
                    self.driver.execute_script(
                        "arguments[0].scrollTop = arguments[0].scrollHeight;",
                        scroll_el,
                    )
                    sleep_random((0.8, 1.6))
        except Exception:
            pass

    def _load_comments(self):
        """Muat komentar tambahan via tombol 'Load more comments' sampai stabil."""
        # Bulk DOM scans must not stall on the implicit wait; explicit
        # sleep_random pauses below give lazy-loaded content time to appear.
        with implicit_wait_off(self.driver):
            max_comments = self._max_comments()
            last_count = -1
            stable_rounds = 0

            for iteration in range(self.MAX_SCROLL_ITERATIONS):
                try:
                    container = self._find_comment_container()
                except Exception:
                    container = None
                rows = self._find_comment_rows(container)
                count = len(rows)

                if count > last_count:
                    stable_rounds = 0
                else:
                    stable_rounds += 1
                last_count = count

                self.logger.info(
                    "[IG] Iterasi %d/%d: %d baris komentar terlihat",
                    iteration + 1,
                    self.MAX_SCROLL_ITERATIONS,
                    count,
                )

                if count >= max_comments:
                    self.logger.info(
                        "[IG] Batas max_comments tercapai (%d).", max_comments
                    )
                    break
                if stable_rounds >= self.STABLE_ROUNDS_LIMIT:
                    self.logger.info(
                        "[IG] Jumlah komentar tidak bertambah, berhenti memuat."
                    )
                    break

                # Scroll elemen scrollable (parent container komentar) untuk
                # memicu lazy-load — satu-satunya mekanisme loading. Klik tombol
                # "Load more comments" dihapus: klik memunculkan popup login dan
                # remove_login_popup_and_overlay mereset state lazy-load sehingga
                # scroll selanjutnya berhenti menambah komentar (terverifikasi
                # lewat A/B test live).
                self._scroll_comment_container()

                sleep_random(self._sec_range())

                if random.random() < self._look_around_probability():
                    try:
                        look_around(self.driver)
                    except Exception as exc:
                        self.logger.debug("[IG] look_around gagal: %s", exc)

                self._check_rate_limit()

            return self._find_comment_rows(self._find_comment_container())

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
                    self.logger.debug("[IG] Baris komentar di-skip: %s", exc)
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

    def _build_comment(self, row: WebElement) -> dict:
        """Ekstrak satu dict komentar dari sebuah baris komentar.

        Selalu mengembalikan seluruh key skema PRD §7; field yang tidak dapat
        diekstrak bernilai None. Tidak pernah raise.
        """
        comment = {
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
            comment["comment_id"] = self._extract_comment_id(row)
        except Exception:
            pass
        try:
            author_name, author_profile_url = self._extract_author(row)
            comment["author_name"] = author_name
            comment["author_profile_url"] = author_profile_url
        except Exception:
            pass
        try:
            comment["comment_time_raw"] = self._extract_time_raw(row)
        except Exception:
            pass
        try:
            comment["comment_text"] = self._extract_comment_text(
                row, comment["author_name"], comment["comment_time_raw"]
            )
        except Exception:
            pass
        try:
            comment["like_count"] = self._extract_like_count(row)
        except Exception:
            pass
        try:
            comment["is_reply"] = self._is_reply(row)
        except Exception:
            pass
        return comment

    @staticmethod
    def _extract_comment_id(row: WebElement) -> Optional[str]:
        """comment_id = segmen numerik setelah /c/ pada link komentar."""
        try:
            for link in row.find_elements(By.CSS_SELECTOR, 'a[href*="/c/"]'):
                href = link.get_attribute("href") or ""
                m = IgScraper.COMMENT_ID_RE.search(href)
                if m:
                    return m.group(1)
        except Exception:
            pass
        return None

    def _extract_author(self, row: WebElement):
        """Kembalikan (author_name, author_profile_url); keduanya bisa None."""
        name = None
        url = None
        # Link profil author: link instagram pertama yang BUKAN link /p/ dan
        # BUKAN link komentar /c/.
        try:
            for link in row.find_elements(By.CSS_SELECTOR, "a[href]"):
                href = (link.get_attribute("href") or "").strip()
                if not href:
                    continue
                if "/c/" in href or "/p/" in href or "/reel/" in href:
                    continue
                url = href
                link_text = self._clean_text(link.text).lstrip("@").strip()
                if link_text:
                    name = link_text
                break
        except Exception:
            pass
        # Fallback nama dari link pertama dengan teks pendek.
        if not name:
            try:
                for link in row.find_elements(By.CSS_SELECTOR, "a[href]"):
                    link_text = self._clean_text(link.text)
                    if link_text and len(link_text) < 60:
                        name = link_text.lstrip("@").strip()
                        break
            except Exception:
                pass
        return (name or None), (url or None)

    def _find_time_element(self, row: WebElement) -> Optional[WebElement]:
        """Kembalikan elemen `time` (atau span waktu relatif) pada baris."""
        try:
            t = row.find_element(By.TAG_NAME, "time")
            try:
                if t.is_displayed():
                    return t
            except Exception:
                return t
        except Exception:
            pass
        try:
            for span in row.find_elements(By.CSS_SELECTOR, "span"):
                text = self._clean_text(span.text)
                if text and len(text) <= 12 and self.RELATIVE_TIME_RE.fullmatch(text):
                    return span
        except Exception:
            pass
        return None

    def _extract_time_raw(self, row: WebElement) -> Optional[str]:
        """Waktu komentar relatif ("1w", "5h", "2 hari") atau None."""
        try:
            time_el = self._find_time_element(row)
            if time_el is not None:
                text = self._clean_text(time_el.text)
                if text and self.RELATIVE_TIME_RE.fullmatch(text):
                    return text
        except Exception:
            pass
        # Link komentar /c/ juga memuat waktu relatif sebagai teksnya.
        try:
            for link in row.find_elements(By.CSS_SELECTOR, 'a[href*="/c/"]'):
                text = self._clean_text(link.text)
                if text and self.RELATIVE_TIME_RE.fullmatch(text):
                    return text
        except Exception:
            pass
        # Fallback: baris teks yang cocok dengan pola waktu relatif.
        try:
            for line in (row.text or "").split("\n"):
                line = line.strip()
                if self.RELATIVE_TIME_RE.fullmatch(line):
                    return line
        except Exception:
            pass
        return None

    def _extract_comment_text(
        self,
        row: WebElement,
        author_name: Optional[str],
        time_raw: Optional[str],
    ) -> Optional[str]:
        """Teks komentar best-effort (None jika ambigu)."""
        # 1) Span yang mengikuti elemen waktu pada baris.
        try:
            time_el = self._find_time_element(row)
            if time_el is not None:
                span = self.driver.execute_script(
                    r"""
                    var el = arguments[0];
                    var n = el.nextElementSibling;
                    for (var i = 0; i < 6 && n; i++) {
                        if (n.tagName.toLowerCase() === 'span') return n;
                        n = n.nextElementSibling;
                    }
                    return null;
                    """,
                    time_el,
                )
                if span is not None:
                    text = self._clean_text(span.text)
                    if text and not self._is_control_line(text):
                        return text
        except Exception:
            pass
        # 2) Span konten pertama: lewati author, waktu, dan baris kontrol.
        try:
            for span in row.find_elements(By.CSS_SELECTOR, "span"):
                text = self._clean_text(span.text)
                if not text:
                    continue
                if author_name and text == author_name:
                    continue
                if time_raw and text == time_raw:
                    continue
                if self._is_control_line(text):
                    continue
                return text
        except Exception:
            pass
        # 3) Fallback dari baris teks row.text.
        try:
            lines = [l.strip() for l in (row.text or "").split("\n") if l.strip()]
            if len(lines) < 2:
                return None
            end = len(lines)
            while end > 0 and self._is_control_line(lines[end - 1]):
                end -= 1
            body = lines[:end]
            if not body:
                return None
            rest = body[1:]  # baris pertama = author
            if rest:
                first = rest[0]
                if (time_raw and first == time_raw) or self.RELATIVE_TIME_RE.fullmatch(first):
                    rest = rest[1:]
            text = " ".join(rest).strip()
            return text or None
        except Exception:
            pass
        return None

    def _extract_like_count(self, row: WebElement) -> Optional[int]:
        """like_count dari baris "^([\\d.,]+) like$"; "Like" tanpa angka -> None."""
        try:
            for line in (row.text or "").split("\n"):
                m = self.LIKE_LINE_RE.fullmatch(line.strip())
                if m:
                    return self._to_int(m.group(1))
        except Exception:
            pass
        return None

    def _is_reply(self, row: WebElement) -> bool:
        """Deteksi baris balasan yang masih ter-collapse (penanda "View replies")."""
        try:
            text = row.text or ""
            if self.VIEW_REPLIES_RE.search(text):
                return True
        except Exception:
            pass
        return False

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _is_control_line(text: str) -> bool:
        """Baris UI yang bukan bagian dari teks komentar."""
        low = (text or "").strip().lower()
        if not low:
            return False
        if low in ("like", "reply"):
            return True
        if re.fullmatch(r"[\d.,]+\s+like", low):
            return True
        if IgScraper.VIEW_REPLIES_RE.search(low):
            return True
        return False

    @staticmethod
    def _clean_text(text) -> str:
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _to_int(value: Optional[str]) -> Optional[int]:
        if not value:
            return None
        try:
            return int(str(value).replace(".", "").replace(",", ""))
        except Exception:
            return None
