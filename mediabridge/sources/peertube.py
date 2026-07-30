"""PeerTube source.

The most reliable default: PeerTube is the only major open video platform that
exposes the licence as a *structured enum* rather than free text, so filtering
happens server-side and cannot be defeated by inconsistent wording.

Blender's open movies live on ``video.blender.org``, which is itself a
PeerTube instance -- no dedicated adapter is needed for them.
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import Field

from ..models import FETCH_YTDLP, MediaItem
from ..utils.text import strip_html
from .base import Source, SourceOptions

log = logging.getLogger(__name__)

#: PeerTube's licence enum, from ``GET /api/v1/videos/licences``.
LICENCES: dict[int, tuple[str, str]] = {
    1: ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
    2: ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"),
    3: ("CC BY-ND 4.0", "https://creativecommons.org/licenses/by-nd/4.0/"),
    4: ("CC BY-NC 4.0", "https://creativecommons.org/licenses/by-nc/4.0/"),
    5: ("CC BY-NC-SA 4.0", "https://creativecommons.org/licenses/by-nc-sa/4.0/"),
    6: ("CC BY-NC-ND 4.0", "https://creativecommons.org/licenses/by-nc-nd/4.0/"),
    7: ("CC0 / Public Domain Dedication", "https://creativecommons.org/publicdomain/zero/1.0/"),
    8: (
        "Free of known copyright restrictions",
        "https://creativecommons.org/publicdomain/mark/1.0/",
    ),
    9: ("All rights reserved", ""),
}

#: Everything except NonCommercial (4/5/6) and All Rights Reserved (9).
DEFAULT_LICENCES = [1, 2, 3, 7, 8]


class PeerTubeOptions(SourceOptions):
    host: str = "video.blender.org"
    channel: str | None = None
    """Channel handle, ``name`` or ``name@host``. Omit to list the whole instance."""

    search: str | None = None
    licence_allow: list[int] = Field(default_factory=lambda: list(DEFAULT_LICENCES))
    is_local: bool = True
    """Restrict to videos hosted by this instance rather than federated copies."""

    nsfw: bool = False
    limit: int = 5


class PeerTubeSource(Source):
    type_name = "peertube"
    options_model = PeerTubeOptions
    description = "PeerTube instances. Licence is a structured enum, so filtering is exact."

    options: PeerTubeOptions

    def _base(self) -> str:
        host = self.options.host.strip().rstrip("/")
        return host if host.startswith("http") else f"https://{host}"

    def _list_url(self) -> str:
        if self.options.channel:
            return f"{self._base()}/api/v1/video-channels/{self.options.channel}/videos"
        if self.options.search:
            return f"{self._base()}/api/v1/search/videos"
        return f"{self._base()}/api/v1/videos"

    def discover(self) -> list[MediaItem]:
        params: dict[str, object] = {
            "count": min(max(self.options.limit * 3, 10), 100),
            "sort": "-publishedAt",
            "nsfw": "true" if self.options.nsfw else "false",
        }
        if self.options.search:
            params["search"] = self.options.search
        if self.options.is_local and not self.options.channel:
            params["isLocal"] = "true"
        if self.options.licence_allow:
            # Current PeerTube releases reject the comma-joined form with
            # "Should have a valid licenceOneOf array" and require repeated
            # bracketed keys, on both instance and channel endpoints.
            params["licenceOneOf[]"] = [str(v) for v in self.options.licence_allow]

        payload = self.get_json(self._list_url(), params=params)
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            log.warning("[%s] unexpected PeerTube response shape", self.name)
            return []

        allowed = set(self.options.licence_allow)
        items: list[MediaItem] = []
        for entry in entries:
            if len(items) >= self.options.limit:
                break
            licence_id = (entry.get("licence") or {}).get("id")
            # Re-check client-side: some instances ignore licenceOneOf.
            if allowed and licence_id not in allowed:
                continue
            item = self._build(entry)
            if item:
                items.append(item)

        log.info("[%s] PeerTube returned %d candidate(s)", self.name, len(items))
        return items

    def _build(self, entry: dict) -> MediaItem | None:
        uuid = entry.get("uuid")
        if not uuid:
            return None

        short_uuid = entry.get("shortUUID") or uuid
        host = self._base()
        licence_id = (entry.get("licence") or {}).get("id")
        licence_name, licence_url = LICENCES.get(
            licence_id, ((entry.get("licence") or {}).get("label", ""), "")
        )

        detail = self._detail(uuid) or entry
        account = detail.get("account") or {}
        channel = detail.get("channel") or {}

        published_at = None
        if raw_date := detail.get("publishedAt"):
            try:
                published_at = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
            except ValueError:
                pass

        thumbnail = detail.get("previewPath") or detail.get("thumbnailPath") or ""
        if thumbnail and not thumbnail.startswith("http"):
            thumbnail = f"{host}{thumbnail}"

        return self.make_item(
            id=str(uuid),
            title=(detail.get("name") or "").strip(),
            webpage_url=f"{host}/w/{short_uuid}",
            description=strip_html(detail.get("description") or ""),
            author=account.get("displayName") or channel.get("displayName") or account.get("name") or "",
            license=licence_name,
            license_url=licence_url,
            duration=detail.get("duration"),
            published_at=published_at,
            thumbnail_url=thumbnail,
            tags=[str(t) for t in (detail.get("tags") or [])],
            filesize_approx=_largest_file_size(detail),
            # The pseudo-URL form works on any instance, including ones absent
            # from yt-dlp's hardcoded host list.
            download_url=f"peertube:{self.options.host.replace('https://', '').rstrip('/')}:{uuid}",
            fetch_strategy=FETCH_YTDLP,
            extra={"licence_id": licence_id},
        )

    def _detail(self, uuid: str) -> dict | None:
        """Fetch the full record for untruncated description, tags and file sizes."""
        try:
            payload = self.get_json(f"{self._base()}/api/v1/videos/{uuid}")
            return payload if isinstance(payload, dict) else None
        except Exception as exc:  # noqa: BLE001 - the list entry is a usable fallback
            log.debug("[%s] could not load PeerTube detail for %s: %s", self.name, uuid, exc)
            return None


def _largest_file_size(detail: dict) -> int | None:
    """Largest resolution's byte size, as a worst-case download estimate."""
    sizes: list[int] = []
    for group in detail.get("files") or []:
        if isinstance(group, dict) and isinstance(group.get("size"), int):
            sizes.append(group["size"])
    for playlist in detail.get("streamingPlaylists") or []:
        for file_entry in playlist.get("files") or []:
            if isinstance(file_entry, dict) and isinstance(file_entry.get("size"), int):
                sizes.append(file_entry["size"])
    return max(sizes) if sizes else None
