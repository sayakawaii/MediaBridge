"""Horizon daily-briefing source (article channel).

`Horizon <https://github.com/sayakawaii/Horizon>`_ is an AI news radar that
publishes one digest per day per language as a GitHub Pages site plus an Atom
feed. Each digest groups items into ``<section class="cat cat-*">`` blocks, and
every item is a ``<details class="hz-item" data-score="8.0">`` element whose
score is the AI relevance rating documented in Horizon's ``docs/scoring.md``
(0-10; the site itself only publishes items at or above 7.0).

That score is the one useful filter here. A digest routinely carries 70+ items,
far more than makes a readable AcFun article, so `min_score` trims it to the
genuinely notable ones instead of truncating arbitrarily.

Two structural mismatches with AcFun are handled here:

* AcFun has no collapsible element, so each ``<details>`` is inlined -- see
  `mediabridge.utils.acfun_html`.
* AcFun strips anchors, so Horizon's citations are rewritten as visible URLs.
  Without that the digest would arrive with every source link removed.

Licensing is the operator's call: Horizon's *code* is MIT, but a digest is an
AI summary of third-party reporting. `discover` therefore refuses to run until
`license_name` is set explicitly, rather than guessing on the user's behalf.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from xml.etree import ElementTree

import requests
from pydantic import Field

from ..errors import SourceError
from ..models import FETCH_NONE, MediaItem
from ..utils.acfun_html import flatten_for_acfun
from ..utils.text import strip_html
from .base import Source, SourceOptions

log = logging.getLogger(__name__)

ATOM_NS = "{http://www.w3.org/2005/Atom}"
DEFAULT_SITE = "https://sayakawaii.github.io/Horizon"
FEED_BY_LANG = {"zh": "feed-zh.xml", "en": "feed-en.xml"}

#: Horizon's own publication floor (docs/scoring.md); anything lower never
#: reaches the feed, so a lower setting here silently does nothing.
HORIZON_PUBLISH_FLOOR = 7.0

FEED_TIMEOUT_SEC = 120


class HorizonOptions(SourceOptions):
    lang: str = "zh"
    """``zh`` or ``en``; selects the default feed when `feed_url` is unset."""

    feed_url: str = ""
    """Full Atom feed URL. Overrides `lang`; set this for a self-hosted Horizon."""

    site_url: str = DEFAULT_SITE

    min_score: float = 8.0
    """Keep only items whose ``data-score`` is at least this."""

    max_entries: int = 15
    """Cap on items included in one article, after score sorting."""

    categories: list[str] = Field(default_factory=list)
    """Optional category-slug allowlist, e.g. ``[ai, geopolitics]``. Empty = all."""

    include_context: bool = True
    """Keep the ``背景`` / ``Context`` enrichment paragraph."""

    include_references: bool = False
    """Keep the nested reference-link list. Verbose; off by default."""

    license_name: str = ""
    """Required. How the digest is licensed, as you have determined it."""

    license_url: str = ""
    cover_url: str = ""
    limit: int = 1
    """Number of daily digests (newest first) to consider per run."""


class HorizonSource(Source):
    type_name = "horizon"
    options_model = HorizonOptions
    description = "Horizon AI daily briefing (article channel). Requires an explicit license_name."

    options: HorizonOptions

    def discover(self) -> list[MediaItem]:
        if not self.options.license_name.strip():
            raise SourceError(
                f"[{self.name}] horizon requires an explicit 'license_name'. A Horizon digest is an "
                "AI summary of third-party reporting, so MediaBridge will not guess whether you may "
                "repost it. Set options.license_name once you have decided."
            )
        if self.options.min_score < HORIZON_PUBLISH_FLOOR:
            log.warning(
                "[%s] min_score=%.1f is below Horizon's own publication floor of %.1f; "
                "no additional items exist below it.",
                self.name,
                self.options.min_score,
                HORIZON_PUBLISH_FLOOR,
            )

        entries = self._fetch_entries()
        items: list[MediaItem] = []

        for entry in entries[: max(self.options.limit, 1)]:
            item = self._build_item(entry)
            if item:
                items.append(item)

        log.info("[%s] Horizon produced %d digest article(s)", self.name, len(items))
        return items

    # -- feed -------------------------------------------------------------

    @property
    def feed_url(self) -> str:
        if self.options.feed_url:
            return self.options.feed_url
        filename = FEED_BY_LANG.get(self.options.lang.lower())
        if not filename:
            raise SourceError(
                f"[{self.name}] unsupported lang {self.options.lang!r}; "
                f"use one of {sorted(FEED_BY_LANG)} or set feed_url explicitly."
            )
        return f"{self.options.site_url.rstrip('/')}/{filename}"

    def _fetch_entries(self) -> list[dict]:
        url = self.feed_url
        try:
            # A digest feed carries every item of the last week inline and runs
            # to ~0.5 MB, which is slow to pull from GitHub Pages on a poor link.
            response = self.session.get(url, timeout=FEED_TIMEOUT_SEC)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SourceError(f"[{self.name}] could not fetch Horizon feed {url}: {exc}") from exc

        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise SourceError(f"[{self.name}] Horizon feed {url} is not valid Atom: {exc}") from exc

        entries = []
        for node in root.findall(f"{ATOM_NS}entry"):
            link_node = node.find(f"{ATOM_NS}link")
            entries.append(
                {
                    "id": _text(node, "id"),
                    "title": _text(node, "title"),
                    "updated": _text(node, "updated"),
                    "link": (link_node.get("href") if link_node is not None else "") or "",
                    "content": _text(node, "content"),
                }
            )

        entries.sort(key=lambda e: e.get("updated") or "", reverse=True)
        return entries

    # -- item construction -------------------------------------------------

    def _build_item(self, entry: dict) -> MediaItem | None:
        digest = parse_digest(entry.get("content") or "")
        if not digest.items:
            log.warning("[%s] digest %s contained no scored items", self.name, entry.get("link"))
            return None

        wanted = {c.strip().lower() for c in self.options.categories if c.strip()}
        selected = [
            it
            for it in digest.items
            if it.score >= self.options.min_score and (not wanted or it.category_slug in wanted)
        ]
        selected.sort(key=lambda it: it.score, reverse=True)
        selected = selected[: max(self.options.max_entries, 1)]

        # Selection is by score, but rendering is grouped by category: sorting
        # the article itself by score would scatter one category's items across
        # the page and repeat its heading.
        order: dict[str, int] = {}
        for position, digest_item in enumerate(digest.items):
            order.setdefault(digest_item.category_slug, position)
        selected.sort(key=lambda it: (order.get(it.category_slug, 0), -it.score))

        if not selected:
            log.info(
                "[%s] %s: none of %d items reached min_score=%.1f",
                self.name,
                entry.get("link"),
                len(digest.items),
                self.options.min_score,
            )
            return None

        link = entry.get("link") or entry.get("id") or self.options.site_url
        body_html = self._render_body(selected, digest, link)

        published_at = _parse_updated(entry.get("updated") or "")
        date_label = published_at.strftime("%Y-%m-%d") if published_at else ""
        title = (
            f"Horizon 每日简报 {date_label}".strip()
            if self.options.lang == "zh"
            else (f"Horizon Daily Briefing {date_label}".strip())
        )

        top = selected[0]
        description = strip_html(top.summary_html)[:200]

        return self.make_item(
            id=entry.get("id") or link,
            title=title,
            webpage_url=link,
            description=description,
            author="Horizon",
            license=self.options.license_name,
            license_url=self.options.license_url,
            published_at=published_at,
            thumbnail_url=self.options.cover_url,
            tags=[],
            fetch_strategy=FETCH_NONE,
            body_html=body_html,
            extra={
                "score_threshold": self.options.min_score,
                "items_selected": len(selected),
                "items_available": len(digest.items),
            },
        )

    def _render_body(self, selected: list[DigestItem], digest: Digest, link: str) -> str:
        zh = self.options.lang == "zh"
        parts: list[str] = []

        # `intro` and item titles arrive as decoded text, so they have to be
        # re-escaped before going back into HTML; a headline containing "&" or
        # "<" would otherwise be reparsed as markup by the flattener.
        if digest.intro:
            parts.append(f"<p>{escape(digest.intro)}</p>")
        parts.append(
            f"<p>本文自动搬运自 Horizon 每日简报，仅收录评分 ≥ {self.options.min_score:g} 的条目"
            f"（共 {len(selected)}/{len(digest.items)} 条）。原文：{link}</p>"
            if zh
            else f"<p>Reposted from the Horizon daily briefing, limited to items scoring "
            f"{self.options.min_score:g} or higher ({len(selected)} of {len(digest.items)}). "
            f"Original: {link}</p>"
        )
        parts.append("<hr/>")

        current_category = ""
        for entry_item in selected:
            if entry_item.category and entry_item.category != current_category:
                current_category = entry_item.category
                parts.append(f"<h2>{escape(entry_item.category)}</h2>")

            # No "⭐️" here: AcFun deletes emoji server-side, so the marker
            # would arrive as a stray space.
            score = f"（{entry_item.score:.1f}/10）" if zh else f" [{entry_item.score:.1f}/10]"
            parts.append(f"<h3>{escape(entry_item.title)}{score}</h3>")
            parts.append(entry_item.summary_html)
            if self.options.include_context and entry_item.context_html:
                parts.append(entry_item.context_html)
            if self.options.include_references and entry_item.references_html:
                parts.append(entry_item.references_html)

        return flatten_for_acfun("".join(parts))


# ---------------------------------------------------------------------------
# Digest HTML parsing
# ---------------------------------------------------------------------------


class DigestItem:
    __slots__ = ("score", "title", "category", "category_slug", "html")

    def __init__(self, score: float, title: str, category: str, category_slug: str, html: str) -> None:
        self.score = score
        self.title = title
        self.category = category
        self.category_slug = category_slug
        self.html = html

    @property
    def summary_html(self) -> str:
        """Body paragraphs, minus the enrichment block and nested references."""
        html = _NESTED_DETAILS.sub("", self.html)
        html = _CONTEXT_PARA.sub("", html)
        html = _SUMMARY_EL.sub("", html)
        return _OUTER_DETAILS.sub("", html).strip()

    @property
    def context_html(self) -> str:
        match = _CONTEXT_PARA.search(_NESTED_DETAILS.sub("", self.html))
        return match.group(0) if match else ""

    @property
    def references_html(self) -> str:
        match = _NESTED_DETAILS.search(self.html)
        return match.group(0) if match else ""


class Digest:
    def __init__(self, intro: str, items: list[DigestItem]) -> None:
        self.intro = intro
        self.items = items


# `背景`/`Context` is emitted as `<p><strong>背景</strong>: ...</p>`.
_CONTEXT_PARA = re.compile(r"<p>\s*<strong>\s*(?:背景|Context)\s*</strong>.*?</p>", re.IGNORECASE | re.DOTALL)
_NESTED_DETAILS = re.compile(r"<details(?![^>]*data-score).*?</details>", re.IGNORECASE | re.DOTALL)
_SUMMARY_EL = re.compile(r"<summary\b.*?</summary>", re.IGNORECASE | re.DOTALL)
_OUTER_DETAILS = re.compile(r"^\s*<details\b[^>]*>|</details>\s*$", re.IGNORECASE)
_CAT_SLUG = re.compile(r"\bcat-([A-Za-z0-9_-]+)")
_SECTION_COUNT = re.compile(r"\s*\(\d+\)\s*$")


class _DigestParser(HTMLParser):
    """Extracts scored `hz-item` blocks along with their section headings."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[DigestItem] = []
        self.intro = ""
        self._category = ""
        self._category_slug = ""
        self._in_heading = 0
        self._heading_buf: list[str] = []
        self._in_blockquote = 0
        self._intro_buf: list[str] = []
        # Raw capture of the current top-level <details data-score=...>.
        self._capture: list[str] | None = None
        self._depth = 0
        self._score = 0.0
        self._title_buf: list[str] = []
        self._in_title = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {k.lower(): (v or "") for k, v in attrs}
        classes = attributes.get("class", "")

        if self._capture is not None:
            if tag == "details":
                self._depth += 1
            if tag == "span" and "hz-item-title" in classes:
                self._in_title += 1
            self._capture.append(self.get_starttag_text() or "")
            return

        if tag == "section" and "cat" in classes.split():
            slug = _CAT_SLUG.search(classes)
            self._category_slug = slug.group(1).lower() if slug else ""
            self._category = ""
            return
        if tag in ("h1", "h2", "h3") and self._category_slug and not self._category:
            self._in_heading += 1
            self._heading_buf = []
            return
        if tag == "blockquote" and not self.items:
            self._in_blockquote += 1
            return
        if tag == "details" and "data-score" in attributes:
            self._capture = [self.get_starttag_text() or ""]
            self._depth = 1
            self._score = _to_float(attributes["data-score"])
            self._title_buf = []
            self._in_title = 0

    def handle_endtag(self, tag: str) -> None:
        if self._capture is not None:
            self._capture.append(f"</{tag}>")
            if tag == "span" and self._in_title:
                self._in_title -= 1
            if tag == "details":
                self._depth -= 1
                if self._depth == 0:
                    self._finish_item()
            return

        if tag in ("h1", "h2", "h3") and self._in_heading:
            self._in_heading -= 1
            self._category = _SECTION_COUNT.sub("", "".join(self._heading_buf).strip())
        elif tag == "blockquote" and self._in_blockquote:
            self._in_blockquote -= 1
            if not self.intro:
                self.intro = " ".join("".join(self._intro_buf).split())
            self._intro_buf = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture is not None:
            self._capture.append(self.get_starttag_text() or "")
            return
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture.append(data)
            if self._in_title:
                self._title_buf.append(data)
            return
        if self._in_heading:
            self._heading_buf.append(data)
        elif self._in_blockquote:
            self._intro_buf.append(data)

    def _finish_item(self) -> None:
        html = "".join(self._capture or [])
        title = " ".join("".join(self._title_buf).split())
        if not title:
            match = _SUMMARY_EL.search(html)
            title = strip_html(match.group(0)) if match else ""
        self.items.append(
            DigestItem(
                score=self._score,
                title=title,
                category=self._category,
                category_slug=self._category_slug,
                html=html,
            )
        )
        self._capture = None
        self._title_buf = []


def parse_digest(content_html: str) -> Digest:
    """Split one Horizon digest into its scored items."""
    parser = _DigestParser()
    parser.feed(content_html or "")
    parser.close()
    return Digest(intro=parser.intro, items=parser.items)


# ---------------------------------------------------------------------------


def _text(node, tag: str) -> str:
    found = node.find(f"{ATOM_NS}{tag}")
    return (found.text or "") if found is not None else ""


def _to_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_updated(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)
