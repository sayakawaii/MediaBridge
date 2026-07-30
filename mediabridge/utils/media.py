"""ffmpeg/ffprobe helpers for container normalisation and cover extraction."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from ..errors import FetchError

log = logging.getLogger(__name__)

#: Containers AcFun accepts directly. Anything else has to be transcoded.
ACFUN_CONTAINERS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".flv",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".vob",
    ".rmvb",
}

COVER_MAX_WIDTH = 1920


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    log.debug("exec: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def probe(path: Path) -> dict:
    """Return ffprobe's JSON description of a media file."""
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=120,
    )
    if result.returncode != 0:
        raise FetchError(f"ffprobe failed for {path.name}: {result.stderr.strip()[:200]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FetchError(f"ffprobe returned invalid JSON for {path.name}") from exc


def duration_seconds(path: Path) -> int | None:
    try:
        value = probe(path).get("format", {}).get("duration")
        return int(float(value)) if value else None
    except (FetchError, TypeError, ValueError):
        return None


def has_data_near_end(path: Path, duration: int, margin: float = 6.0) -> bool:
    """Check that real packets exist close to the end of the stream.

    A truncated MP4 still reports its full duration, because that comes from
    the header, so duration alone cannot distinguish a complete download from
    a partial one. Seeking to the tail and asking for actual packets does --
    and it costs a tenth of a second, versus seconds for a full decode.
    """
    if not have_ffmpeg() or duration <= 0:
        return False

    start = max(duration - margin, 0)
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time",
            "-read_intervals",
            f"{start}%+#2",
            "-of",
            "csv=p=0",
            str(path),
        ],
        timeout=120,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def ensure_uploadable(path: Path, work_dir: Path) -> Path:
    """Return a path AcFun will accept, transcoding only when necessary.

    Wikimedia Commons serves mostly ``.ogv`` and ``.webm``, neither of which
    AcFun ingests, so those get a real transcode. Everything already in an
    accepted container is passed through untouched -- re-encoding costs CPU
    minutes on a 2-core runner and degrades quality for no benefit.
    """
    if path.suffix.lower() in ACFUN_CONTAINERS:
        return path

    if not have_ffmpeg():
        raise FetchError(
            f"{path.name} is in an unsupported container ({path.suffix}) and ffmpeg is not installed.",
            hint="Install ffmpeg, or choose a source that serves MP4.",
        )

    target = work_dir / f"{path.stem}.mp4"
    log.info("Transcoding %s to MP4 (this is CPU-bound and may take a while)", path.name)
    result = _run(
        [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-i",
            str(path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(target),
        ]
    )
    if result.returncode != 0 or not target.is_file():
        raise FetchError(f"Transcode of {path.name} failed: {result.stderr.strip()[-400:]}")

    path.unlink(missing_ok=True)  # Reclaim disk immediately; runners are tight.
    return target


def extract_cover(video_path: Path, target: Path, at_seconds: float = 3.0) -> Path | None:
    """Grab a single frame to use as a cover when the source provides none."""
    if not have_ffmpeg():
        return None

    duration = duration_seconds(video_path)
    if duration and at_seconds >= duration:
        at_seconds = max(duration / 2, 0)

    result = _run(
        [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-ss",
            str(at_seconds),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-vf",
            f"scale='min({COVER_MAX_WIDTH},iw)':-2",
            str(target),
        ],
        timeout=300,
    )
    if result.returncode != 0 or not target.is_file():
        log.warning("Could not extract a cover frame from %s", video_path.name)
        return None
    return target


def normalise_cover(path: Path, work_dir: Path) -> Path:
    """Convert a downloaded thumbnail to a JPEG of sane dimensions."""
    if not have_ffmpeg():
        return path

    target = work_dir / "cover.jpg"
    if path.resolve() == target.resolve():
        target = work_dir / "cover_normalised.jpg"

    result = _run(
        [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-vf",
            f"scale='min({COVER_MAX_WIDTH},iw)':-2",
            str(target),
        ],
        timeout=180,
    )
    if result.returncode != 0 or not target.is_file():
        log.debug("Cover normalisation failed, using the original file")
        return path
    return target
