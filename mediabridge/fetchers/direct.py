"""Direct HTTP fetcher.

Used by sources that already know the exact media URL (Internet Archive,
Wikimedia Commons, NASA). Going straight to the file avoids yt-dlp's format
negotiation and, more importantly, lets the size be checked from
``Content-Length`` before committing runner disk to the download.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from ..config import LimitsConfig
from ..errors import FetchError, SkipItem
from ..models import FetchedMedia, MediaItem
from ..utils.http import build_session
from ..utils.media import ensure_uploadable, normalise_cover
from ..utils.text import safe_filename

log = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024

MEDIA_TIMEOUT = (30, 300)

#: A cover is a few hundred kilobytes at most, so a stalled one is a stalled
#: connection, not a slow file. Waiting the media read timeout for it just burns
#: runner minutes on something the publish step can do without.
COVER_TIMEOUT = (15, 60)


def _extension_from_url(url: str, default: str = ".mp4") -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    suffix = Path(name).suffix.lower()
    return suffix if 1 < len(suffix) <= 6 else default


def download_to(
    url: str,
    target_stem: Path,
    max_bytes: int | None = None,
    session=None,
    timeout: tuple[int, int] = MEDIA_TIMEOUT,
) -> Path:
    """Stream `url` to disk, aborting as soon as `max_bytes` is exceeded."""
    session = session or build_session()
    target = target_stem.with_suffix(_extension_from_url(url, target_stem.suffix or ".bin"))

    try:
        with session.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()

            declared = response.headers.get("content-length")
            if declared and max_bytes and int(declared) > max_bytes:
                raise SkipItem(
                    f"Server reports {int(declared) / 1048576:.0f} MiB, over the "
                    f"{max_bytes / 1048576:.0f} MiB budget"
                )

            written = 0
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    # Not every server sends Content-Length, so enforce the
                    # budget as the bytes actually arrive too.
                    if max_bytes and written > max_bytes:
                        handle.close()
                        target.unlink(missing_ok=True)
                        raise SkipItem(
                            f"Download exceeded the {max_bytes / 1048576:.0f} MiB budget mid-transfer"
                        )
    except requests.RequestException as exc:
        target.unlink(missing_ok=True)
        raise FetchError(f"Download failed for {url}: {exc}") from exc

    if not target.is_file() or target.stat().st_size == 0:
        raise FetchError(f"Download produced an empty file for {url}")

    log.info("Downloaded %s (%.1f MiB)", target.name, target.stat().st_size / 1048576)
    return target


def fetch_direct(item: MediaItem, work_dir: Path, limits: LimitsConfig) -> FetchedMedia:
    if not item.download_url:
        raise FetchError(f"Source '{item.source_name}' produced no download_url for {item.webpage_url}")

    session = build_session()
    max_bytes = int(limits.max_filesize_mb) * 1048576 if limits.max_filesize_mb else None

    stem = work_dir / safe_filename(item.title, fallback="media")
    video_path = download_to(item.download_url, stem, max_bytes=max_bytes, session=session)
    video_path = ensure_uploadable(video_path, work_dir)

    cover_path = None
    if item.thumbnail_url:
        try:
            raw = download_to(
                item.thumbnail_url, work_dir / "cover_raw", session=session, timeout=COVER_TIMEOUT
            )
            cover_path = normalise_cover(raw, work_dir)
        except (FetchError, SkipItem) as exc:
            log.warning("Could not fetch the supplied cover (%s); will extract a frame", exc)

    if item.duration is None:
        from ..utils.media import duration_seconds

        item.duration = duration_seconds(video_path)

    return FetchedMedia(item=item, video_path=video_path, cover_path=cover_path)
