"""Generic RSS/Atom source for the article channel.

Most newsrooms that permit reposting publish an ordinary feed, so one
configurable source covers them all rather than a plugin per outlet. Two body
strategies are needed in practice:

``content``
    The feed already carries the article, in ``content:encoded`` (RSS) or
    ``<content>`` (Atom). NASA's feeds work this way.

``scrape``
    The feed carries only a one-line summary and the article lives on the page.
    UN News works this way, and its terms require reposting *in full*, so the
    summary alone would not satisfy them. `body_class` names the container to
    lift out of the page.

Two things are deliberately not guessed:

* **Licensing.** ``license_name`` is mandatory, exactly as in the Horizon
  source. A feed being public says nothing about whether you may repost it, and
  most news feeds you may not.
* **Images.** A licence that covers text often excludes photography -- UN text
  may be reproduced freely while UN photographs may not -- so ``strip_images``
  exists to drop them rather than silently republishing someone's picture.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import requests
from pydantic import Field

from ..errors import SourceError
from ..models import FETCH_NONE, MediaItem
from ..utils.acfun_html import flatten_for_acfun
from ..utils.text import strip_html
from .base import Source, SourceOptions

log = logging.getLogger(__name__)

ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"

FEED_TIMEOUT_SEC = 60
PAGE_TIMEOUT_SEC = 60

#: `flatten_for_acfun` emits images in exactly this shape, so dropping them
#: afterwards is an exact match rather than a guess at arbitrary markup.
_FLAT_IMAGE = re.compile(r"<p><img src=\"[^\"]*\"></p>")

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_DIV_TAG = re.compile(r"<(/?)div\b", re.IGNORECASE)


def extract_container(html: str, class_substring: str) -> str:
    """Return the inner HTML of the first ``<div>`` whose class contains the marker.

    Counting nested ``<div>`` beats a non-greedy regex, which stops at the first
    ``</div>`` and silently truncates the article to its opening paragraphs.
    """
    if not class_substring:
        return ""
    body = _COMMENT.sub("", html)

    for opening in re.finditer(r"<div\b[^>]*>", body, re.IGNORECASE):
        if class_substring not in opening.group(0):
            continue
        depth = 1
        cursor = opening.end()
        for tag in _DIV_TAG.finditer(body, cursor):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                return body[cursor : tag.start()]
        # Unbalanced markup: take the rest rather than nothing.
        return body[cursor:]
    return ""


class FeedOptions(SourceOptions):
    url: str = ""
    """The RSS or Atom feed. Required."""

    body: str = "content"
    """``content`` to use the feed's own article body, ``scrape`` to fetch the page."""

    body_class: str = ""
    """For ``scrape``: a substring of the class on the div wrapping the article."""

    include_summary: bool = True
    """Prepend the feed summary. It is usually the lead paragraph, absent from the body."""

    prefer_guid_link: bool = False
    """Cite ``guid`` rather than ``link``. Some feeds, UN News among them, point
    ``link`` at a click-tracking redirect and keep the canonical URL in ``guid``."""

    strip_images: bool = False
    """Drop images. Set this whenever the licence covers text but not photography."""

    include_categories: list[str] = Field(default_factory=list)
    exclude_categories: list[str] = Field(default_factory=list)
    """Category filters, matched case-insensitively against the feed's own labels."""

    min_body_chars: int = 200
    """Skip entries whose extracted body is shorter than this; usually a failed scrape."""

    max_body_chars: int = 40000

    author: str = ""
    """Attribution label, e.g. 联合国新闻. Falls back to the feed title."""

    license_name: str = ""
    """Required. How this feed is licensed for reposting, as you have determined it."""

    license_url: str = ""
    cover_url: str = ""
    limit: int = 3


class FeedSource(Source):
    type_name = "feed"
    options_model = FeedOptions
    description = "Generic RSS/Atom article source. Requires an explicit license_name."

    options: FeedOptions

    def discover(self) -> list[MediaItem]:
        opts = self.options
        if not opts.url.strip():
            raise SourceError(f"[{self.name}] feed requires options.url")
        if not opts.license_name.strip():
            raise SourceError(
                f"[{self.name}] feed requires an explicit 'license_name'. A feed being public "
                "says nothing about whether you may repost it, and most news feeds you may not. "
                "Set options.license_name once you have checked the outlet's terms."
            )
        if opts.body not in ("content", "scrape"):
            raise SourceError(f"[{self.name}] options.body must be 'content' or 'scrape'")
        if opts.body == "scrape" and not opts.body_class.strip():
            raise SourceError(f"[{self.name}] body='scrape' needs options.body_class")

        root = self._fetch_feed(opts.url)
        feed_title = self._feed_title(root)

        items: list[MediaItem] = []
        for entry in self._entries(root):
            if len(items) >= opts.limit:
                break
            try:
                item = self._build(entry, feed_title)
            except SourceError as exc:
                log.warning("%s", exc)
                continue
            if item is not None:
                items.append(item)

        if not items:
            log.warning("[%s] no usable entries in %s", self.name, opts.url)
        return items

    # ---- feed plumbing ---------------------------------------------------

    def _fetch_feed(self, url: str) -> ElementTree.Element:
        try:
            response = self.session.get(url, timeout=FEED_TIMEOUT_SEC)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SourceError(f"[{self.name}] could not fetch {url}: {exc}") from exc
        try:
            return ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise SourceError(f"[{self.name}] {url} is not valid RSS or Atom: {exc}") from exc

    @staticmethod
    def _entries(root: ElementTree.Element) -> list[ElementTree.Element]:
        return root.findall(".//item") or root.findall(f".//{ATOM}entry")

    @staticmethod
    def _feed_title(root: ElementTree.Element) -> str:
        for path in ("./channel/title", f"./{ATOM}title"):
            node = root.find(path)
            if node is not None and node.text:
                return node.text.strip()
        return ""

    @staticmethod
    def _text(entry: ElementTree.Element, *paths: str) -> str:
        for path in paths:
            node = entry.find(path)
            if node is not None and (node.text or "").strip():
                return node.text.strip()
        return ""

    @staticmethod
    def _link(entry: ElementTree.Element, prefer_guid: bool = False) -> str:
        if prefer_guid:
            guid = entry.find("guid")
            text = (guid.text or "").strip() if guid is not None else ""
            if text.startswith("http"):
                return text
        node = entry.find("link")
        if node is not None:
            if node.text and node.text.strip():
                return node.text.strip()
        for candidate in entry.findall(f"{ATOM}link"):
            rel = candidate.get("rel", "alternate")
            if rel == "alternate" and candidate.get("href"):
                return candidate.get("href", "")
        guid = entry.find("guid")
        return (guid.text or "").strip() if guid is not None else ""

    @staticmethod
    def _published(entry: ElementTree.Element) -> datetime | None:
        raw = FeedSource._text(entry, "pubDate", f"{ATOM}updated", f"{ATOM}published")
        if not raw:
            return None
        try:
            return parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _categories(entry: ElementTree.Element) -> list[str]:
        out = [(node.text or "").strip() for node in entry.findall("category")]
        out += [node.get("term", "") for node in entry.findall(f"{ATOM}category")]
        return [value for value in out if value]

    # ---- item construction ----------------------------------------------

    def _passes_categories(self, categories: list[str]) -> bool:
        opts = self.options
        lowered = {value.lower() for value in categories}
        if opts.exclude_categories and lowered & {v.lower() for v in opts.exclude_categories}:
            return False
        if opts.include_categories:
            return bool(lowered & {v.lower() for v in opts.include_categories})
        return True

    def _scrape_body(self, url: str) -> str:
        try:
            response = self.session.get(url, timeout=PAGE_TIMEOUT_SEC)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SourceError(f"[{self.name}] could not fetch article {url}: {exc}") from exc
        response.encoding = response.encoding or "utf-8"
        return extract_container(response.text, self.options.body_class)

    def _build(self, entry: ElementTree.Element, feed_title: str) -> MediaItem | None:
        opts = self.options

        title = self._text(entry, "title", f"{ATOM}title")
        url = self._link(entry, opts.prefer_guid_link)
        if not title or not url:
            return None
        if not self._passes_categories(self._categories(entry)):
            return None

        summary = strip_html(self._text(entry, "description", f"{ATOM}summary"))

        if opts.body == "content":
            raw_body = self._text(entry, f"{CONTENT_NS}encoded", f"{ATOM}content", "description")
        else:
            raw_body = self._scrape_body(url)

        body = flatten_for_acfun(raw_body)
        if opts.strip_images:
            body = _FLAT_IMAGE.sub("", body)

        lead = ""
        if opts.include_summary and summary and summary[:24] not in strip_html(body):
            lead = f"<p>{summary}</p>"
        body = lead + body

        plain = strip_html(body)
        if len(plain) < opts.min_body_chars:
            log.info("[%s] skipping '%s': only %d characters of body", self.name, title[:40], len(plain))
            return None
        if len(body) > opts.max_body_chars:
            raise SourceError(f"[{self.name}] '{title[:40]}' body exceeds max_body_chars")

        guid = self._text(entry, "guid", f"{ATOM}id") or url
        published = self._published(entry)

        return self.make_item(
            id=guid,
            title=title,
            webpage_url=url,
            description=summary or plain[:180],
            author=opts.author or feed_title,
            license=opts.license_name,
            license_url=opts.license_url,
            published_at=published or datetime.now(timezone.utc),
            body_html=body,
            fetch_strategy=FETCH_NONE,
            thumbnail_url=opts.cover_url,
        )
