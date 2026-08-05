"""Input/output utilities (FR-1, FR-6).

Reads URL lists, validates/parses per-platform URLs, extracts post IDs,
and writes structured JSON results with the standard filename scheme.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from src.logger import get_logger

log = get_logger("io_utils")

_FB_KEYWORD_SEGMENTS = frozenset(
    {"posts", "videos", "reels", "permalink", "story", "watch", "notes", "photo", "p"}
)
_IG_KEYWORD_SEGMENTS = frozenset({"p", "reel", "reels", "tv"})


def is_valid_url(url: str, platform: str) -> bool:
    """Return True if ``url`` is a valid http(s) URL for ``platform``.

    - ``fb``: hostname contains ``facebook.com`` or ``fb.watch``.
    - ``ig``: hostname contains ``instagram.com``.
    - Rejects non-http(s) schemes and malformed URLs.
    """
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc or not host:
        return False

    if platform == "fb":
        return "facebook.com" in host or "fb.watch" in host
    if platform == "ig":
        return "instagram.com" in host
    return False


def _extract_fb_post_id(segments: list[str], host: str) -> str:
    """Extract the numeric post id from Facebook URL path segments."""
    if "fb.watch" in host:
        # fb.watch shortcode is the first path segment.
        return segments[0] if segments else ""

    # Prefer a numeric segment that follows a known keyword, e.g. /posts/123.
    for i, seg in enumerate(segments):
        if seg.lower() in _FB_KEYWORD_SEGMENTS and i + 1 < len(segments):
            candidate = segments[i + 1]
            if candidate.isdigit():
                return candidate

    # Fallback: the trailing numeric segment anywhere in the path.
    numerics = [seg for seg in segments if seg.isdigit()]
    if numerics:
        return numerics[-1]

    # Last resort: the last non-empty path segment.
    return segments[-1] if segments else ""


def _extract_ig_post_id(segments: list[str]) -> str:
    """Extract the shortcode from Instagram URL path segments."""
    for i, seg in enumerate(segments):
        if seg.lower() in _IG_KEYWORD_SEGMENTS and i + 1 < len(segments):
            return segments[i + 1]
    return segments[-1] if segments else ""


def extract_post_id(url: str, platform: str) -> str:
    """Extract a post id (numeric for FB, shortcode for IG) from ``url``.

    - FB: trailing numeric id after ``/posts/``, ``/videos/``, ... or the
      fb.watch shortcode; falls back to the last non-empty path segment.
    - IG: the path segment after ``/p/``, ``/reel/``, ``/tv/``, ``/reels/``;
      falls back to the last non-empty path segment.

    Trailing slashes are ignored.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    path = parsed.path or ""
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return ""

    if platform == "fb":
        return _extract_fb_post_id(segments, (parsed.hostname or "").lower())
    if platform == "ig":
        return _extract_ig_post_id(segments)
    return segments[-1]


def read_urls_from_file(path: str, platform: str) -> list[str]:
    """Read and validate a list of URLs from a text file.

    - Blank lines and lines starting with ``#`` are ignored.
    - Invalid lines (wrong domain / malformed) are skipped with a warning.
    - Returns the list of valid URLs in file order.
    """
    valid: list[str] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.error("Could not read URL file %s: %s", path, exc)
        return []

    for idx, raw in enumerate(lines, start=1):
        url = raw.strip()
        if not url or url.startswith("#"):
            continue
        if not is_valid_url(url, platform):
            log.warning(
                "Skipping invalid %s URL on line %d: %s",
                platform.upper(), idx, url,
            )
            continue
        valid.append(url)

    log.info("Loaded %d valid URL(s) from %s", len(valid), path)
    return valid


def build_filename(post_id: str, platform: str, now: datetime, time_format: str) -> str:
    """Build the output filename: ``<post_id>-<PLATFORM>-<timestamp>.json``."""
    return f"{post_id}-{platform.upper()}-{now.strftime(time_format)}.json"


def ensure_output_dir(base_dir: str, platform: str) -> Path:
    """Create and return ``Path(base_dir) / platform`` (parents created)."""
    out_dir = Path(base_dir) / platform
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def write_json(data: dict, output_path: Path) -> Path:
    """Write ``data`` as pretty JSON without logging (for incremental saves)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return output_path


def save_json_result(data: dict, output_path: Path) -> Path:
    """Write ``data`` as pretty JSON (``ensure_ascii=False, indent=2``).

    Returns the ``output_path`` as a ``Path``.
    """
    output_path = write_json(data, output_path)
    log.info("Saved result to %s", output_path)
    return output_path
