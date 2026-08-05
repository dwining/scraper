"""CLI orchestrator for the FB/IG public comment scraper (PRD §5, §8, §9, §13).

Parses command-line arguments, loads ``config.yaml``, drives a single
Selenium session through a batch of URLs (one platform per run, no parallel
requests), saves per-post JSON results, and prints a final summary report.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from src.browser_utils import load_config, setup_driver
from src.exceptions import LoginRequiredError, RateLimitError
from src.fb_scraper import FbScraper
from src.human_behavior import sleep_random
from src.ig_scraper import IgScraper
from src.io_utils import (
    build_filename,
    ensure_output_dir,
    extract_post_id,
    read_urls_from_file,
    save_json_result,
    write_json,
)
from src.logger import get_logger, setup_logging

CONFIG_PATH = "config.yaml"

LEGAL_EPILOG = (
    "PERINGATAN LEGAL & ETIKA: Scraping melanggar Terms of Service Meta "
    "(Facebook & Instagram). Aplikasi ini hanya untuk riset internal skala "
    "kecil pada konten yang SUDAH PUBLIK - bukan untuk scraping massal/komersial. "
    "Alternatif yang aman dan sesuai ToS: Meta Graph API / Instagram Graph API."
)


def parse_args() -> argparse.Namespace:
    """Parse and validate the CLI arguments (PRD §9)."""
    parser = argparse.ArgumentParser(
        description=(
            "Scrape komentar publik dari postingan Facebook atau Instagram "
            "(satu platform per eksekusi) menggunakan Selenium."
        ),
        epilog=LEGAL_EPILOG,
    )
    parser.add_argument(
        "--platform",
        choices=["fb", "ig"],
        required=True,
        help="Platform target: 'fb' (Facebook) atau 'ig' (Instagram).",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path ke file .txt berisi daftar URL (satu URL per baris).",
    )
    parser.add_argument(
        "--output-dir",
        help="Override folder output (default: config output.base_dir).",
    )
    parser.add_argument(
        "--max-comments",
        type=int,
        help="Override batas komentar per postingan (default: config scraping.max_comments_per_post).",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--headful",
        action="store_true",
        help="Jalankan browser terlihat (default; mode 'browser testing').",
    )
    mode_group.add_argument(
        "--headless",
        action="store_true",
        help="Jalankan browser headless (hanya untuk debugging; tidak direkomendasikan).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # ------------------------------------------------------------------ #
    # Config + CLI overrides
    # ------------------------------------------------------------------ #
    try:
        config = load_config(CONFIG_PATH)
    except Exception as exc:
        print(f"[CRITICAL] Gagal memuat konfigurasi {CONFIG_PATH}: {exc}", file=sys.stderr)
        return 1

    if args.output_dir:
        config.setdefault("output", {})["base_dir"] = args.output_dir
    if args.max_comments is not None:
        config.setdefault("scraping", {})["max_comments_per_post"] = args.max_comments
    if args.headless:
        config.setdefault("browser", {})["headless"] = True

    headless = bool(config["browser"]["headless"])
    max_comments = config["scraping"]["max_comments_per_post"]

    log_dir = config.get("logging", {}).get("log_dir", "logs")
    setup_logging(log_dir)
    log = get_logger("main")

    log.info(
        "Startup: platform=%s input=%s headless=%s max_comments=%s output_dir=%s",
        args.platform,
        args.input,
        headless,
        max_comments,
        config["output"]["base_dir"],
    )

    # ------------------------------------------------------------------ #
    # Input validation (FR-1)
    # ------------------------------------------------------------------ #
    if not Path(args.input).is_file():
        log.warning("File input tidak ditemukan: %s", args.input)
        return 1

    urls = read_urls_from_file(args.input, args.platform)
    if not urls:
        log.warning(
            "Tidak ada URL valid untuk diproses di %s (platform=%s). Keluar.",
            args.input,
            args.platform,
        )
        return 1

    # ------------------------------------------------------------------ #
    # One browser session for the whole batch (PRD §11 point 6: never parallel)
    # ------------------------------------------------------------------ #
    try:
        driver = setup_driver(config, headless=headless)
    except Exception as exc:
        log.critical("Gagal meluncurkan browser (Chrome): %s", exc)
        return 1

    scraping_cfg = config["scraping"]
    max_retry = int(scraping_cfg.get("max_retry", 2))
    retry_delay_sec = float(scraping_cfg.get("retry_delay_sec", 5))
    rate_limit_cooldown_min = float(scraping_cfg.get("rate_limit_cooldown_min", 30))
    between_urls_sec = tuple(config.get("delays", {}).get("between_urls_sec", (8, 20)))

    ScraperClass = FbScraper if args.platform == "fb" else IgScraper
    scraper = ScraperClass(driver, config)

    total = len(urls)
    success = 0
    skipped_login = 0
    failed = 0
    rate_limited = False
    rate_limited_count = 0
    outcomes: list[tuple[str, str, int, str]] = []

    try:
        for url in urls:
            log.info("Processing URL: %s", url)

            status = ""
            error_msg = ""
            n_comments = 0
            result: dict | None = None

            # Compute the fixed output path once per URL so the live file has a
            # predictable name, and wire the incremental-save callback.
            post_id = extract_post_id(url, args.platform)
            now = datetime.now().astimezone()
            filename = build_filename(
                post_id,
                args.platform,
                now,
                config["output"]["filename_time_format"],
            )
            out_dir = ensure_output_dir(
                config["output"]["base_dir"], args.platform
            )
            output_path = out_dir / filename
            if hasattr(scraper, "on_progress"):
                scraper.on_progress = (
                    lambda partial, path=output_path: write_json(partial, path)
                )

            # Retry loop (PRD FR-8): attempt 0..max_retry.
            for attempt in range(max_retry + 1):
                try:
                    result = scraper.scrape_post(url)
                    status = "success"
                    n_comments = int(result.get("total_comments_scraped", 0) or 0)
                    break
                except LoginRequiredError as exc:
                    # Never retried: private/restricted content (FR-4).
                    status = "skipped_login_required"
                    error_msg = str(exc)
                    break
                except RateLimitError as exc:
                    # Never retried: stop the whole batch (FR-8).
                    status = "failed_rate_limit"
                    error_msg = str(exc)
                    rate_limited = True
                    rate_limited_count = 1
                    break
                except Exception as exc:
                    # ScrapeError and any other unexpected Exception share
                    # the same retry path (PRD FR-8).
                    error_msg = str(exc)
                    if attempt < max_retry:
                        log.warning(
                            "Attempt %d/%d gagal untuk %s: %s. Retry dalam %ss...",
                            attempt + 1,
                            max_retry + 1,
                            url,
                            exc,
                            retry_delay_sec,
                        )
                        time.sleep(retry_delay_sec)
                    else:
                        status = "failed"

            # ------------------------------------------------------------------ #
            # Per-URL post-processing (save / count / log)
            # ------------------------------------------------------------------ #
            if status == "success" and result is not None:
                try:
                    output_path = save_json_result(result, output_path)
                    success += 1
                    log.info(
                        "Sukses: %d komentar dari %s -> %s",
                        n_comments,
                        url,
                        output_path,
                    )
                except Exception as exc:
                    status = "failed"
                    error_msg = f"gagal menyimpan hasil: {exc}"
                    failed += 1
                    log.error("Gagal menyimpan hasil untuk %s: %s", url, exc)
            elif status == "skipped_login_required":
                skipped_login += 1
                log.warning("Skip (login required) untuk %s: %s", url, error_msg)
            elif status == "failed_rate_limit":
                log.error(
                    "Rate limit detected: %s. Stopping entire batch.", error_msg
                )
            elif status == "failed":
                failed += 1
                log.error(
                    "Gagal setelah %d percobaan untuk %s: %s",
                    max_retry + 1,
                    url,
                    error_msg,
                )

            outcomes.append((url, status, n_comments, error_msg))

            if rate_limited:
                break

            sleep_random(between_urls_sec)
    finally:
        driver.quit()

    # ------------------------------------------------------------------ #
    # Rate-limit cooldown (batch already stopped, PRD FR-8)
    # ------------------------------------------------------------------ #
    if rate_limited:
        log.warning(
            "PROSES DIHENTIKAN untuk menghindari pemblokiran akun/IP lebih lanjut "
            "(rate-limit terdeteksi). Cooldown selama %.0f menit sebelum keluar.",
            rate_limit_cooldown_min,
        )
        time.sleep(rate_limit_cooldown_min * 60.0)
        log.info("Cooldown selesai.")

    # ------------------------------------------------------------------ #
    # Summary report (PRD FR-7)
    # ------------------------------------------------------------------ #
    print("===== RINGKASAN =====")
    print(f"Total URL     : {total}")
    print(f"Sukses        : {success}")
    print(f"Skip (login)  : {skipped_login}")
    print(f"Gagal (error) : {failed}")
    print(f"Rate-limit    : {rate_limited_count}")
    print("=====================")

    # ------------------------------------------------------------------ #
    # Exit code
    # ------------------------------------------------------------------ #
    if rate_limited:
        return 2
    if success > 0 or (success == 0 and skipped_login > 0):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
