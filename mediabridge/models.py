"""The data contract between sources and publishers.

`MediaItem` is deliberately the only type both halves of the pipeline know
about. A source never learns which platform it is feeding, and a publisher
never learns where an item came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# How the orchestrator should turn `MediaItem` into local files.
FETCH_YTDLP = "ytdlp"
FETCH_DIRECT = "direct"
FETCH_NONE = "none"  # Article-style items that carry their payload inline.


@dataclass
class MediaItem:
    """One candidate for reposting, normalised across every source."""

    source_name: str
    """User-assigned name of the configured source entry (dedup namespace)."""

    source_type: str
    """Registered adapter type, e.g. ``peertube``."""

    id: str
    """Stable identifier within the source. Must not change between runs."""

    title: str
    webpage_url: str

    description: str = ""
    author: str = ""

    license: str = ""
    """Human-readable licence name, e.g. ``CC BY-SA 4.0``."""

    license_url: str = ""

    duration: int | None = None
    """Length in seconds, when the source reports it."""

    published_at: datetime | None = None
    thumbnail_url: str = ""
    tags: list[str] = field(default_factory=list)

    filesize_approx: int | None = None
    """Best pre-download size estimate in bytes, used to protect runner disk."""

    download_url: str | None = None
    """Direct media URL, set when the source can bypass yt-dlp."""

    fetch_strategy: str = FETCH_YTDLP

    body_html: str = ""
    """Inline article payload, only used by article-style items."""

    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        return f"{self.source_name}:{self.id}"

    def summary(self) -> str:
        bits = [self.title]
        if self.duration:
            bits.append(f"{self.duration // 60}m{self.duration % 60:02d}s")
        if self.license:
            bits.append(self.license)
        return " | ".join(bits)


@dataclass
class FetchedMedia:
    """Local artefacts produced for a `MediaItem` before publishing."""

    item: MediaItem
    video_path: Path | None = None
    cover_path: Path | None = None
    info: dict[str, Any] = field(default_factory=dict)
    """Raw extractor metadata, kept for templating and debugging."""

    @property
    def size_bytes(self) -> int:
        return self.video_path.stat().st_size if self.video_path else 0


@dataclass
class PublishResult:
    """Outcome of handing one item to a publisher."""

    ok: bool
    remote_id: str = ""
    url: str = ""
    message: str = ""
    dry_run: bool = False
