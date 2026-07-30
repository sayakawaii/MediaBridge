"""Pre-download gatekeeping.

Everything here runs before a single byte is fetched. GitHub-hosted runners
only guarantee 14 GB of free disk, and a 17 GB Internet Archive item will fill
it long before the upload stage is reached.
"""

from __future__ import annotations

import logging

from ..config import LimitsConfig
from ..models import MediaItem

log = logging.getLogger(__name__)


def check(item: MediaItem, limits: LimitsConfig) -> str:
    """Return a human-readable rejection reason, or an empty string to accept."""
    if item.duration is not None:
        if limits.max_duration_sec and item.duration > limits.max_duration_sec:
            return f"duration {item.duration}s exceeds max_duration_sec={limits.max_duration_sec}"
        if item.duration < limits.min_duration_sec:
            return f"duration {item.duration}s below min_duration_sec={limits.min_duration_sec}"

    if item.filesize_approx and limits.max_filesize_mb:
        size_mb = item.filesize_approx / 1048576
        if size_mb > limits.max_filesize_mb:
            return f"estimated size {size_mb:.0f} MiB exceeds max_filesize_mb={limits.max_filesize_mb}"

    return ""


def ytdlp_match_filter(limits: LimitsConfig) -> str:
    """Build the ``--match-filter`` expression for yt-dlp.

    This is the real defence against oversized downloads: ``--max-filesize``
    cannot reliably abort fragmented HLS/DASH streams part-way through, whereas
    a match filter rejects the item before extraction begins.
    """
    clauses = ["!is_live"]
    if limits.max_duration_sec:
        clauses.append(f"duration < {limits.max_duration_sec}")
    if limits.min_duration_sec:
        clauses.append(f"duration >= {limits.min_duration_sec}")
    if limits.max_filesize_mb:
        clauses.append(f"filesize_approx <? {int(limits.max_filesize_mb) * 1048576}")
    return " & ".join(clauses)
