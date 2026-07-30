"""yt-dlp fetcher."""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import LimitsConfig
from ..errors import FetchError, SkipItem
from ..filters.limits import ytdlp_match_filter
from ..models import FetchedMedia, MediaItem
from ..utils.media import ensure_uploadable, normalise_cover

log = logging.getLogger(__name__)

#: Prefer H.264 + AAC so the result is already in AcFun's recommended shape and
#: needs no re-encode. The trailing fallbacks accept anything rather than
#: failing outright on sources that offer nothing else.
FORMAT_SELECTOR = (
    "bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/b[ext=mp4][height<=1080]/bv*[height<=1080]+ba/b"
)


def fetch_ytdlp(item: MediaItem, work_dir: Path, limits: LimitsConfig) -> FetchedMedia:
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError, match_filter_func
    except ImportError as exc:  # pragma: no cover
        raise FetchError("yt-dlp is not installed; run `pip install yt-dlp`.") from exc

    target = item.download_url or item.webpage_url
    options = {
        "outtmpl": str(work_dir / "media.%(ext)s"),
        "format": FORMAT_SELECTOR,
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 2,
        "socket_timeout": 60,
        "continuedl": True,
        "logger": _YtdlpLogger(),
        # This, not --max-filesize, is the real guard: fragmented HLS/DASH
        # downloads cannot be reliably aborted once they have started.
        "match_filter": match_filter_func(ytdlp_match_filter(limits)),
        "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}],
    }

    log.info("Downloading %s via yt-dlp", target)
    info: dict | None = None
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(target, download=True)
    except DownloadError as exc:
        message = str(exc)
        if "does not pass filter" in message or "skipping" in message.lower():
            raise SkipItem(f"yt-dlp rejected {target} on the match filter: {message}") from exc

        # A stalled connection near the end of a large download can leave
        # yt-dlp reporting failure over an already-complete file. Throwing
        # away hundreds of megabytes over post-download bookkeeping is worse
        # than checking whether the file is actually intact.
        salvaged = _salvage(work_dir, item)
        if not salvaged:
            raise FetchError(f"yt-dlp failed for {target}: {message}") from exc
        log.warning(
            "yt-dlp reported an error (%s) but the downloaded file is complete; continuing.",
            message,
        )
        info = {}

    if info is None:
        raise SkipItem(f"yt-dlp returned no metadata for {target}")

    video_path = _locate_media(work_dir)
    if not video_path:
        raise FetchError(f"yt-dlp reported success but produced no media file for {target}")

    video_path = ensure_uploadable(video_path, work_dir)

    cover_path = _locate_cover(work_dir)
    if cover_path:
        cover_path = normalise_cover(cover_path, work_dir)

    _backfill(item, info)
    return FetchedMedia(item=item, video_path=video_path, cover_path=cover_path, info=info)


class _YtdlpLogger:
    """Route yt-dlp's chatter into our logging hierarchy."""

    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        # yt-dlp routes interpreter deprecation notices through error(), which
        # would otherwise look like a failed download in the Actions log.
        if "Deprecated Feature" in msg:
            log.debug("yt-dlp: %s", msg)
        else:
            log.error("yt-dlp: %s", msg)


_MEDIA_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv", ".avi", ".ogv")
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


#: A salvaged file must be within this fraction of the advertised duration.
_DURATION_TOLERANCE = 0.02


def _salvage(work_dir: Path, item: MediaItem) -> bool:
    """Decide whether a file left behind by a failed run is safe to publish.

    Only accepted when its real duration matches what the source advertised;
    without that check this would happily upload a truncated download.
    """
    if any(p.suffix == ".part" for p in work_dir.iterdir()):
        return False

    candidate = _locate_media(work_dir)
    if not candidate or candidate.stat().st_size == 0:
        return False

    if not item.duration:
        log.debug("Cannot salvage %s: the source advertised no duration to verify against", candidate.name)
        return False

    from ..utils.media import duration_seconds, has_data_near_end

    actual = duration_seconds(candidate)
    if actual is None:
        return False

    drift = abs(actual - item.duration) / item.duration
    if drift > _DURATION_TOLERANCE:
        log.info(
            "Discarding partial download: %ds on disk vs %ds expected (%.0f%% off)",
            actual,
            item.duration,
            drift * 100,
        )
        return False

    if not has_data_near_end(candidate, actual):
        log.info("Discarding partial download: %s has no readable data near its end", candidate.name)
        return False

    return True


def _locate_media(work_dir: Path) -> Path | None:
    files = [p for p in work_dir.iterdir() if p.is_file() and p.suffix.lower() in _MEDIA_EXTENSIONS]
    return max(files, key=lambda p: p.stat().st_size) if files else None


def _locate_cover(work_dir: Path) -> Path | None:
    files = [p for p in work_dir.iterdir() if p.is_file() and p.suffix.lower() in _IMAGE_EXTENSIONS]
    return max(files, key=lambda p: p.stat().st_size) if files else None


def _backfill(item: MediaItem, info: dict) -> None:
    """Fill gaps the source could not populate from the extractor's metadata."""
    if not item.duration and info.get("duration"):
        item.duration = int(info["duration"])
    if not item.description and info.get("description"):
        item.description = str(info["description"])
    if not item.author:
        item.author = str(info.get("uploader") or info.get("channel") or "")
    if not item.license and info.get("license"):
        item.license = str(info["license"])
    if not item.tags and info.get("tags"):
        item.tags = [str(t) for t in info["tags"]][:8]
