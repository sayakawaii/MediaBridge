"""Fetcher dispatch.

A fetcher turns a `MediaItem` into local files. Which one runs is decided by
`MediaItem.fetch_strategy`, so a source picks its download path without the
orchestrator needing to know anything about it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import LimitsConfig
from ..errors import FetchError, SkipItem
from ..models import FETCH_DIRECT, FETCH_NONE, FETCH_YTDLP, FetchedMedia, MediaItem
from ..utils.media import extract_cover, normalise_cover

log = logging.getLogger(__name__)


def fetch(item: MediaItem, work_dir: Path, limits: LimitsConfig) -> FetchedMedia:
    """Download `item` into `work_dir` using its declared strategy."""
    work_dir.mkdir(parents=True, exist_ok=True)

    if item.fetch_strategy == FETCH_NONE:
        return _fetch_inline(item, work_dir)
    if item.fetch_strategy == FETCH_DIRECT:
        from .direct import fetch_direct

        fetched = fetch_direct(item, work_dir, limits)
    elif item.fetch_strategy == FETCH_YTDLP:
        from .ytdlp import fetch_ytdlp

        fetched = fetch_ytdlp(item, work_dir, limits)
    else:
        raise FetchError(f"Unknown fetch strategy '{item.fetch_strategy}' for {item.webpage_url}")

    _ensure_cover(fetched, work_dir)
    _enforce_size(fetched, limits)
    return fetched


def _fetch_inline(item: MediaItem, work_dir: Path) -> FetchedMedia:
    """Article-style items carry their payload; only a cover may be needed."""
    fetched = FetchedMedia(item=item)
    if item.thumbnail_url:
        from .direct import COVER_TIMEOUT, download_to

        try:
            raw = download_to(item.thumbnail_url, work_dir / "cover_raw", timeout=COVER_TIMEOUT)
            fetched.cover_path = normalise_cover(raw, work_dir)
        except FetchError as exc:
            # AcFun only requires title, channel and creationType for an
            # article, so a missing cover is a cosmetic loss, not a failure.
            log.warning("Could not fetch cover for %s: %s", item.webpage_url, exc)
    return fetched


def _ensure_cover(fetched: FetchedMedia, work_dir: Path) -> None:
    """AcFun refuses submissions without a cover, so synthesise one if needed."""
    if fetched.cover_path and fetched.cover_path.is_file():
        return
    if not fetched.video_path:
        return

    log.info("No cover supplied by the source; extracting a frame instead")
    frame = extract_cover(fetched.video_path, work_dir / "cover.jpg")
    if frame:
        fetched.cover_path = frame


def _enforce_size(fetched: FetchedMedia, limits: LimitsConfig) -> None:
    """Final gate: pre-download estimates are frequently absent or wrong."""
    if not fetched.video_path or not limits.max_filesize_mb:
        return
    size_mb = fetched.size_bytes / 1048576
    if size_mb > limits.max_filesize_mb:
        fetched.video_path.unlink(missing_ok=True)
        raise SkipItem(f"Downloaded file is {size_mb:.0f} MiB, over max_filesize_mb={limits.max_filesize_mb}")
