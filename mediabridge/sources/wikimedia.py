"""Wikimedia Commons source.

Commons is attractive because every file carries reviewed, structured licence
metadata in ``extmetadata``. The catch is the container: most videos are
``.ogv`` or ``.webm``, neither of which AcFun accepts, so the fetcher has to
remux or transcode to MP4 before upload.

Cover images need an explicit ``iiurlwidth``; without it the API returns no
``thumburl`` for video files.

Commons also enforces a user-agent policy that the shared browser string fails:
the API answers a generic ``Mozilla/5.0`` with HTTP 403 and a pointer to the
robot policy. This source therefore identifies itself by name instead.
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import Field

from .. import __version__
from ..filters import licenses
from ..models import FETCH_DIRECT, MediaItem
from ..utils.text import strip_html
from .base import Source, SourceOptions

log = logging.getLogger(__name__)

API_URL = "https://commons.wikimedia.org/w/api.php"
THUMB_WIDTH = 1280

#: https://foundation.wikimedia.org/wiki/Policy:User-Agent_policy asks for a
#: tool name and a way to make contact.
WIKIMEDIA_USER_AGENT = f"MediaBridge/{__version__} (https://github.com/sayakawaii/MediaBridge)"

#: Only the extmetadata fields we actually read. Requesting the full set makes
#: the response several times larger for no benefit.
EXTMETADATA_FIELDS = "|".join(
    ["LicenseShortName", "UsageTerms", "Artist", "ImageDescription", "LicenseUrl", "DateTimeOriginal"]
)


class WikimediaOptions(SourceOptions):
    search: str = "nasa"
    """Search terms; ``filetype:video`` is added automatically."""

    license_allow: list[str] = Field(default_factory=list)
    limit: int = 3


class WikimediaSource(Source):
    type_name = "wikimedia"
    options_model = WikimediaOptions
    description = "Wikimedia Commons. Reviewed licences, but files usually need transcoding to MP4."

    options: WikimediaOptions

    def discover(self) -> list[MediaItem]:
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": f"filetype:video {self.options.search}".strip(),
            "gsrnamespace": "6",
            "gsrlimit": str(min(max(self.options.limit * 4, 10), 50)),
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata|mediatype|user",
            "iiextmetadatafilter": EXTMETADATA_FIELDS,
            "iiurlwidth": str(THUMB_WIDTH),
            "format": "json",
            "formatversion": "2",
        }

        self.session.headers["User-Agent"] = WIKIMEDIA_USER_AGENT
        payload = self.get_json(API_URL, params=params)
        pages = ((payload or {}).get("query") or {}).get("pages") or []

        allowed = licenses.parse_allowlist(self.options.license_allow)
        items: list[MediaItem] = []

        for page in pages:
            if len(items) >= self.options.limit:
                break
            info_list = page.get("imageinfo") or []
            if not info_list:
                continue
            info = info_list[0]
            meta = info.get("extmetadata") or {}

            license_name = _meta(meta, "LicenseShortName") or _meta(meta, "UsageTerms")
            license_url = _meta(meta, "LicenseUrl")
            if not licenses.is_allowed(license_name, license_url, allowed):
                log.debug(
                    "[%s] skipping %s: licence %r not allowed",
                    self.name,
                    page.get("title"),
                    license_name,
                )
                continue

            item = self._build(page, info, meta, license_name, license_url)
            if item:
                items.append(item)

        log.info(
            "[%s] Wikimedia Commons returned %d candidate(s) of %d searched",
            self.name,
            len(items),
            len(pages),
        )
        return items

    def _build(
        self, page: dict, info: dict, meta: dict, license_name: str, license_url: str
    ) -> MediaItem | None:
        title = str(page.get("title") or "")
        url = info.get("url")
        if not url:
            return None

        display_title = title.removeprefix("File:").rsplit(".", 1)[0].replace("_", " ").strip()

        published_at = None
        if raw_date := _meta(meta, "DateTimeOriginal"):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y"):
                try:
                    published_at = datetime.strptime(raw_date[: len(fmt) + 4], fmt)
                    break
                except ValueError:
                    continue

        duration = info.get("duration")
        return self.make_item(
            id=str(page.get("pageid") or title),
            title=display_title or title,
            webpage_url=f"https://commons.wikimedia.org/wiki/{title.replace(' ', '_')}",
            description=strip_html(_meta(meta, "ImageDescription")),
            author=strip_html(_meta(meta, "Artist")) or info.get("user") or "Wikimedia Commons",
            license=license_name or licenses.identify(url=license_url),
            license_url=license_url,
            duration=int(duration) if isinstance(duration, (int, float)) else None,
            published_at=published_at,
            thumbnail_url=info.get("thumburl") or "",
            tags=[],
            filesize_approx=info.get("size"),
            download_url=url,
            fetch_strategy=FETCH_DIRECT,
            extra={"mime": info.get("mime"), "mediatype": info.get("mediatype")},
        )


def _meta(extmetadata: dict, key: str) -> str:
    entry = extmetadata.get(key)
    if isinstance(entry, dict):
        return str(entry.get("value") or "").strip()
    return ""
