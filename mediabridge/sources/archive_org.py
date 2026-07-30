"""Internet Archive source.

Two hazards shape this adapter:

* ``mediatype:movies`` says nothing about copyright. The Archive is a
  user-upload platform and the majority of items carry no ``licenseurl`` at
  all, so the search query hard-requires that field and the result is checked
  against the licence whitelist again on the client side.
* Items can be enormous (17 GB has been observed in the ``nasa`` collection).
  The per-item metadata endpoint reports an exact byte size for every
  derivative, so a rendition that fits the disk budget is chosen *before*
  anything is downloaded.
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import Field

from ..filters import licenses
from ..models import FETCH_DIRECT, MediaItem
from ..utils.text import strip_html
from .base import Source, SourceOptions

log = logging.getLogger(__name__)

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata"
DOWNLOAD_URL = "https://archive.org/download"
THUMBNAIL_URL = "https://archive.org/services/img"

_VIDEO_EXTENSIONS = (".mp4", ".m4v", ".mov", ".mkv", ".avi", ".mpg", ".mpeg", ".ogv", ".webm")
_PREFERRED_EXTENSIONS = (".mp4", ".m4v")

#: Derivatives below this are thumbnails or samples, not the actual work.
_MIN_USEFUL_BYTES = 512 * 1024


class ArchiveOrgOptions(SourceOptions):
    collection: str | None = "prelinger"
    query: str | None = None
    """Extra Lucene clauses ANDed onto the generated query."""

    require_license: bool = True
    """Keep this on. Most Archive items have no licence metadata whatsoever."""

    license_allow: list[str] = Field(default_factory=list)
    prefer: str = "smallest"
    """``smallest`` favours low-bitrate derivatives; ``largest`` maximises quality."""

    limit: int = 3


class ArchiveOrgSource(Source):
    type_name = "archive_org"
    options_model = ArchiveOrgOptions
    description = "Internet Archive collections. Most items lack licence metadata -- filtering is mandatory."

    options: ArchiveOrgOptions

    def _query(self) -> str:
        clauses = ['mediatype:"movies"']
        if self.options.collection:
            clauses.append(f'collection:"{self.options.collection}"')
        if self.options.require_license:
            clauses.append("licenseurl:[* TO *]")
        if self.options.query:
            clauses.append(f"({self.options.query})")
        return " AND ".join(clauses)

    def discover(self) -> list[MediaItem]:
        params = [
            ("q", self._query()),
            ("rows", str(min(max(self.options.limit * 4, 10), 100))),
            ("page", "1"),
            ("output", "json"),
            ("sort[]", "publicdate desc"),
        ]
        for field in (
            "identifier",
            "title",
            "licenseurl",
            "publicdate",
            "item_size",
            "creator",
            "description",
        ):
            params.append(("fl[]", field))

        payload = self.get_json(SEARCH_URL, params=params)
        docs = ((payload or {}).get("response") or {}).get("docs") or []

        allowed = licenses.parse_allowlist(self.options.license_allow)
        items: list[MediaItem] = []

        for doc in docs:
            if len(items) >= self.options.limit:
                break
            license_url = _first(doc.get("licenseurl"))
            if self.options.require_license and not licenses.is_allowed(url=license_url, allowed=allowed):
                log.debug(
                    "[%s] skipping %s: licence %r not allowed",
                    self.name,
                    doc.get("identifier"),
                    license_url,
                )
                continue
            item = self._build(doc, license_url)
            if item:
                items.append(item)

        log.info(
            "[%s] Internet Archive returned %d candidate(s) of %d searched",
            self.name,
            len(items),
            len(docs),
        )
        return items

    def _build(self, doc: dict, license_url: str) -> MediaItem | None:
        identifier = doc.get("identifier")
        if not identifier:
            return None

        metadata = self.get_json(f"{METADATA_URL}/{identifier}")
        chosen = self._choose_file(metadata.get("files") or [])
        if not chosen:
            log.debug("[%s] %s has no usable video derivative", self.name, identifier)
            return None

        meta = metadata.get("metadata") or {}
        published_at = None
        if raw_date := _first(doc.get("publicdate")):
            try:
                published_at = datetime.strptime(raw_date[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                pass

        license_name = licenses.identify(url=license_url) or "见原始链接"

        return self.make_item(
            id=str(identifier),
            title=(_first(doc.get("title")) or identifier).strip(),
            webpage_url=f"https://archive.org/details/{identifier}",
            description=strip_html(_first(doc.get("description")) or _first(meta.get("description")) or ""),
            author=_first(doc.get("creator")) or _first(meta.get("creator")) or "Internet Archive",
            license=license_name,
            license_url=license_url,
            duration=_parse_length(chosen.get("length")),
            published_at=published_at,
            thumbnail_url=f"{THUMBNAIL_URL}/{identifier}",
            tags=[t for t in str(_first(meta.get("subject")) or "").split(";") if t.strip()][:5],
            filesize_approx=int(chosen.get("size") or 0) or None,
            download_url=f"{DOWNLOAD_URL}/{identifier}/{chosen['name']}",
            fetch_strategy=FETCH_DIRECT,
            extra={"format": chosen.get("format"), "file": chosen.get("name")},
        )

    def _choose_file(self, files: list[dict]) -> dict | None:
        """Pick the derivative that best fits the disk budget."""
        candidates = []
        for entry in files:
            name = str(entry.get("name") or "")
            if not name.lower().endswith(_VIDEO_EXTENSIONS):
                continue
            try:
                size = int(entry.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if size < _MIN_USEFUL_BYTES:
                continue
            candidates.append({**entry, "size": size})

        if not candidates:
            return None

        cap = self.max_bytes
        # MP4 first: everything else would need a transcode before upload.
        preferred = [c for c in candidates if str(c["name"]).lower().endswith(_PREFERRED_EXTENSIONS)]
        pool = preferred or candidates

        fitting = [c for c in pool if not cap or c["size"] <= cap]
        if not fitting:
            # Nothing fits; surface the smallest so the limits filter can
            # reject it with an accurate size rather than silently guessing.
            return min(pool, key=lambda c: c["size"])

        return (
            min(fitting, key=lambda c: c["size"])
            if self.options.prefer == "smallest"
            else max(fitting, key=lambda c: c["size"])
        )


def _first(value: object) -> str:
    """Archive search fields are sometimes scalars and sometimes lists."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value is not None else ""


def _parse_length(value: object) -> int | None:
    """Parse Archive durations, which may be ``123.4`` or ``H:MM:SS``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if ":" in text:
            parts = [float(p) for p in text.split(":")]
            seconds = 0.0
            for part in parts:
                seconds = seconds * 60 + part
            return int(seconds)
        return int(float(text))
    except ValueError:
        return None
