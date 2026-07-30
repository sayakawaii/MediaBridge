"""Kuaishou cloud chunked upload, shared by video files and cover images.

AcFun hands out an upload token and then steps aside: the bytes go straight to
``upload.kuaishouzt.com`` in fragments, and only the resulting handle is
reported back to AcFun.
"""

from __future__ import annotations

import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from ...errors import PublishError

log = logging.getLogger(__name__)

FRAGMENT_PATH = "/api/upload/fragment"
COMPLETE_PATH = "/api/upload/complete"

DEFAULT_PART_SIZE = 1024 * 1024
MAX_FRAGMENT_ATTEMPTS = 4


def _upload_user_agent() -> str:
    """User-Agent for the Kuaishou upload gateway.

    The gateway routes on User-Agent, and a browser-like string sends the
    request down a path that rejects it with the thoroughly misleading
    ``Required String parameter 'upload_token' is not present`` -- even though
    the parameters exactly match what AcFun's own web client sends. Strings
    containing ``python-requests/<version>`` are accepted; browser, curl,
    okhttp and bare urllib3 strings are not.

    So we keep requests' own token in the string and prepend our identity,
    rather than sending the browser User-Agent used for the member API.
    """
    from ... import __version__

    return f"MediaBridge/{__version__} {requests.utils.default_user_agent()}"


def _redact(message: str) -> str:
    """Strip upload tokens out of anything destined for a log.

    Transport errors quote the full request URL, and the token in it is a
    live credential. Actions logs are public on public repositories.
    """
    return re.sub(r"(uploadToken|upload_token)=[^&\s'\"]+", r"\1=<redacted>", str(message))


def _endpoint_url(host: str, path: str) -> str:
    host = host.strip().rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    return f"{host}{path}"


def _upload_fragment(
    session: requests.Session,
    host: str,
    upload_token: str,
    payload: bytes,
    *,
    index: int,
    start: int,
    total_size: int,
    timeout: int,
) -> None:
    headers = {
        "Content-Type": "application/octet-stream",
        # The Kuaishou endpoint validates this against the declared file size.
        "Content-Range": f"bytes {start}-{start + len(payload) - 1}/{total_size}",
        "User-Agent": _upload_user_agent(),
    }
    params = {"fragmentId": str(index), "uploadToken": upload_token}
    url = _endpoint_url(host, FRAGMENT_PATH)

    last_error = ""
    for attempt in range(1, MAX_FRAGMENT_ATTEMPTS + 1):
        try:
            response = session.post(url, params=params, data=payload, headers=headers, timeout=timeout)
            if response.status_code == 200:
                body = response.json() if response.content else {}
                if not isinstance(body, dict) or body.get("result", 1) == 1:
                    return
                last_error = f"result={body.get('result')} {body.get('error_msg', '')}"
            else:
                last_error = f"HTTP {response.status_code}: {response.text[:160]}"
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)

        last_error = _redact(last_error)
        if attempt < MAX_FRAGMENT_ATTEMPTS:
            delay = 2**attempt
            log.warning("Fragment %d failed (%s); retrying in %ds", index, last_error[:160], delay)
            time.sleep(delay)

    raise PublishError(f"Fragment {index} failed after {MAX_FRAGMENT_ATTEMPTS} attempts: {last_error[:300]}")


def upload_file(
    session: requests.Session,
    path: Path,
    upload_token: str,
    *,
    host: str = "upload.kuaishouzt.com",
    part_size: int = DEFAULT_PART_SIZE,
    parallel: int = 2,
    timeout: int = 300,
    label: str = "file",
) -> None:
    """Send `path` to the Kuaishou endpoint in fragments, then complete it."""
    total_size = path.stat().st_size
    if total_size <= 0:
        raise PublishError(f"Refusing to upload empty file: {path}")

    part_size = max(int(part_size) or DEFAULT_PART_SIZE, 64 * 1024)
    fragment_count = math.ceil(total_size / part_size)
    parallel = max(1, min(int(parallel or 1), 4, fragment_count))

    log.info(
        "Uploading %s: %.1f MiB in %d fragment(s) of %.0f KiB, %d in parallel",
        label,
        total_size / 1048576,
        fragment_count,
        part_size / 1024,
        parallel,
    )

    started = time.monotonic()
    completed = 0

    def send(index: int) -> None:
        start = index * part_size
        # Each worker opens its own handle so seeks cannot race.
        with path.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read(min(part_size, total_size - start))
        _upload_fragment(
            session,
            host,
            upload_token,
            chunk,
            index=index,
            start=start,
            total_size=total_size,
            timeout=timeout,
        )

    if parallel == 1:
        for index in range(fragment_count):
            send(index)
            completed += 1
            _log_progress(label, completed, fragment_count, total_size, part_size, started)
    else:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            for _ in pool.map(send, range(fragment_count)):
                completed += 1
                _log_progress(label, completed, fragment_count, total_size, part_size, started)

    _complete(session, host, upload_token, fragment_count, timeout)

    elapsed = max(time.monotonic() - started, 0.001)
    log.info(
        "Uploaded %s in %.1fs (%.2f MiB/s)",
        label,
        elapsed,
        total_size / 1048576 / elapsed,
    )


def _log_progress(label: str, done: int, total: int, total_size: int, part_size: int, started: float) -> None:
    # One line per 10% keeps Actions logs readable for multi-gigabyte uploads.
    step = max(1, total // 10)
    if done % step and done != total:
        return
    elapsed = max(time.monotonic() - started, 0.001)
    sent = min(done * part_size, total_size)
    log.info(
        "  %s %d/%d fragments (%.0f%%, %.2f MiB/s)",
        label,
        done,
        total,
        done / total * 100,
        sent / 1048576 / elapsed,
    )


def _complete(
    session: requests.Session,
    host: str,
    upload_token: str,
    fragment_count: int,
    timeout: int,
) -> None:
    url = _endpoint_url(host, COMPLETE_PATH)
    params = {"fragmentCount": str(fragment_count), "uploadToken": upload_token}
    try:
        response = session.post(
            url, params=params, headers={"User-Agent": _upload_user_agent()}, timeout=timeout
        )
    except requests.RequestException as exc:
        raise PublishError(f"Upload completion request failed: {_redact(exc)[:300]}") from exc

    if response.status_code != 200:
        raise PublishError(f"Upload completion failed: HTTP {response.status_code} {response.text[:200]}")

    try:
        body = response.json()
    except ValueError:
        return  # An empty 200 is success for this endpoint.

    if isinstance(body, dict) and body.get("result", 1) != 1:
        raise PublishError(f"Upload completion rejected: {body}")
