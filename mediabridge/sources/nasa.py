"""NASA Image and Video Library source.

The API needs no key and returns rich descriptions plus pre-generated
thumbnails. There is no licence *field*: NASA material is generally not
subject to copyright in the US, which is asserted here as policy rather than
read from metadata.

Two caveats are reflected in the generated attribution: NASA's insignia and
logotype are protected and are not public domain, and NASA pages occasionally
embed third-party copyrighted material. Operators should spot-check.
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote, urlsplit, urlunsplit

from ..models import FETCH_DIRECT, MediaItem
from .base import Source, SourceOptions

log = logging.getLogger(__name__)

SEARCH_URL = "https://images-api.nasa.gov/search"
LICENSE_NAME = "NASA Media Usage (public domain, attribution requested)"
LICENSE_URL = "https://www.nasa.gov/nasa-brand-center/images-and-media/"

#: Renditions from smallest to largest.
_RENDITIONS = ("~mobile.mp4", "~small.mp4", "~medium.mp4", "~large.mp4", "~orig.mp4")


def _encode_url(url: str) -> str:
    """Percent-encode the path. NASA asset URLs contain literal spaces."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe="/~"), parts.query, parts.fragment))


class NasaOptions(SourceOptions):
    search: str = ""
    year_start: int | None = None
    year_end: int | None = None
    keywords: str | None = None

    quality: str = "large"
    """Preferred rendition: mobile, small, medium, large or orig.

    Defaults to ``large`` because AcFun asks for 1080p; drop it to ``medium``
    if runner disk or upload bandwidth becomes the binding constraint.
    """

    limit: int = 3


class NasaSource(Source):
    type_name = "nasa"
    options_model = NasaOptions
    description = "NASA Image and Video Library. Public domain, no API key required."

    options: NasaOptions

    def discover(self) -> list[MediaItem]:
        params: dict[str, object] = {
            "media_type": "video",
            "page_size": min(max(self.options.limit * 4, 10), 100),
        }
        if self.options.search:
            params["q"] = self.options.search
        if self.options.keywords:
            params["keywords"] = self.options.keywords
        if self.options.year_start:
            params["year_start"] = self.options.year_start
        if self.options.year_end:
            params["year_end"] = self.options.year_end

        payload = self.get_json(SEARCH_URL, params=params)
        entries = ((payload or {}).get("collection") or {}).get("items") or []

        items: list[MediaItem] = []
        for entry in entries:
            if len(items) >= self.options.limit:
                break
            item = self._build(entry)
            if item:
                items.append(item)

        log.info("[%s] NASA returned %d candidate(s) of %d searched", self.name, len(items), len(entries))
        return items

    def _build(self, entry: dict) -> MediaItem | None:
        data_list = entry.get("data") or []
        if not data_list:
            return None
        data = data_list[0]
        nasa_id = data.get("nasa_id")
        if not nasa_id:
            return None

        video_url = self._pick_asset(entry.get("href"))
        if not video_url:
            log.debug("[%s] %s exposes no MP4 rendition", self.name, nasa_id)
            return None

        thumbnail = ""
        for link in entry.get("links") or []:
            if link.get("render") == "image" and link.get("href"):
                thumbnail = _encode_url(str(link["href"]))
                break

        published_at = None
        if raw_date := data.get("date_created"):
            try:
                published_at = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
            except ValueError:
                pass

        author = data.get("photographer") or data.get("center") or "NASA"

        return self.make_item(
            id=str(nasa_id),
            title=str(data.get("title") or nasa_id).strip(),
            webpage_url=f"https://images.nasa.gov/details/{quote(str(nasa_id), safe='')}",
            description=str(data.get("description") or data.get("description_508") or "").strip(),
            author=f"NASA/{author}" if author != "NASA" else "NASA",
            license=LICENSE_NAME,
            license_url=LICENSE_URL,
            duration=None,
            published_at=published_at,
            thumbnail_url=thumbnail,
            tags=[str(k) for k in (data.get("keywords") or [])][:5],
            filesize_approx=None,
            download_url=video_url,
            fetch_strategy=FETCH_DIRECT,
            extra={"nasa_id": nasa_id, "center": data.get("center")},
        )

    def _pick_asset(self, collection_href: object) -> str:
        """Resolve the per-item asset manifest to a single MP4 URL."""
        if not collection_href:
            return ""
        try:
            assets = self.get_json(str(collection_href))
        except Exception as exc:  # noqa: BLE001 - one bad item must not abort discovery
            log.debug("[%s] could not read NASA asset manifest: %s", self.name, exc)
            return ""

        if not isinstance(assets, list):
            return ""
        urls = [str(u) for u in assets if isinstance(u, str)]

        for suffix in self._rendition_order():
            for url in urls:
                if url.lower().endswith(suffix):
                    return _encode_url(url.replace("http://", "https://"))

        for url in urls:
            if url.lower().endswith(".mp4"):
                return _encode_url(url.replace("http://", "https://"))
        return ""

    def _rendition_order(self) -> list[str]:
        """Requested tier first, then progressively smaller, then larger."""
        wanted = f"~{self.options.quality.strip().lower()}.mp4"
        if wanted not in _RENDITIONS:
            wanted = "~large.mp4"
        index = _RENDITIONS.index(wanted)
        return [wanted, *reversed(_RENDITIONS[:index]), *_RENDITIONS[index + 1 :]]
