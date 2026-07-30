"""Horizon digest source tests.

The fixture below reproduces the markup Horizon actually emits: categories as
``<section class="cat cat-*">``, items as ``<details data-score>``, and a
nested ``<details>`` holding reference links.
"""

from __future__ import annotations

import re

import pytest

from mediabridge.errors import SourceError
from mediabridge.models import FETCH_NONE
from mediabridge.sources.horizon import HorizonSource, parse_digest
from tests.test_acfun_html import ALLOWED_TAGS, tags_in

DIGEST_HTML = """
<blockquote><p>从 180 条内容中筛选出 3 条重要资讯。</p></blockquote>
<hr />
<section class="cat cat-tech">
  <h2 id="section">🔬 科技 / AI (2)</h2>
  <p><a id="item-1"></a></p>
  <details class="hz-item" data-score="9.0">
    <summary><span class="hz-item-title">High scoring item</span>
      <span class="hz-item-score">⭐️ 9.0/10</span></summary>
    <p>Body of the high scoring item.</p>
    <p>🔗 <a href="https://example.com/high">来源</a></p>
    <p><strong>背景</strong>: Context for the high item.</p>
    <details><summary>参考链接</summary>
      <ul><li><a href="https://ref.example.com/a">Reference A</a></li></ul>
    </details>
  </details>
  <details class="hz-item" data-score="7.0">
    <summary><span class="hz-item-title">Low scoring item</span>
      <span class="hz-item-score">⭐️ 7.0/10</span></summary>
    <p>Body of the low scoring item.</p>
  </details>
</section>
<section class="cat cat-papers">
  <h2 id="section-1">📄 论文精选 (1)</h2>
  <details class="hz-item" data-score="8.5">
    <summary><span class="hz-item-title">Paper item</span>
      <span class="hz-item-score">⭐️ 8.5/10</span></summary>
    <p>Body of the paper item.</p>
  </details>
</section>
"""

FEED_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Horizon Daily - 中文摘要</title>
  <entry>
    <title>Horizon Summary: 2026-07-28 (ZH)</title>
    <link href="https://example.org/Horizon/2026/07/28/summary-zh.html"/>
    <updated>2026-07-28T00:00:00+00:00</updated>
    <id>https://example.org/Horizon/2026/07/28/summary-zh.html</id>
    <content type="html"><![CDATA[ <p>older digest</p> ]]></content>
  </entry>
  <entry>
    <title>Horizon Summary: 2026-07-29 (ZH)</title>
    <link href="https://example.org/Horizon/2026/07/29/summary-zh.html"/>
    <updated>2026-07-29T00:00:00+00:00</updated>
    <id>https://example.org/Horizon/2026/07/29/summary-zh.html</id>
    <content type="html"><![CDATA[{DIGEST_HTML}]]></content>
  </entry>
</feed>
"""


class _FakeSession:
    def __init__(self, body: str = FEED_XML) -> None:
        self.body = body
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return _FakeResponse(self.body.encode("utf-8"))


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        pass


def build(**options) -> HorizonSource:
    options.setdefault("license_name", "见原文各条目来源")
    session = options.pop("session", None) or _FakeSession()
    source = HorizonSource("horizon-daily", options, session=session)
    return source


# --------------------------------------------------------------------------
# Digest parsing


def test_parses_every_scored_item_with_its_category():
    digest = parse_digest(DIGEST_HTML)
    assert digest.intro == "从 180 条内容中筛选出 3 条重要资讯。"
    assert [(i.title, i.score, i.category_slug) for i in digest.items] == [
        ("High scoring item", 9.0, "tech"),
        ("Low scoring item", 7.0, "tech"),
        ("Paper item", 8.5, "papers"),
    ]


def test_section_heading_loses_its_item_count():
    assert parse_digest(DIGEST_HTML).items[0].category == "🔬 科技 / AI"


def test_scores_are_rendered_without_emoji():
    body = build(min_score=8.0).discover()[0].body_html
    assert "<h3>High scoring item（9.0/10）</h3>" in body


def test_nested_reference_details_does_not_end_the_item():
    # A naive scan for the next </details> would truncate the item at the
    # reference list and swallow the rest of the section.
    item = parse_digest(DIGEST_HTML).items[0]
    assert "Context for the high item" in item.context_html
    assert "Reference A" in item.references_html
    assert "Reference A" not in item.summary_html
    assert "背景" not in item.summary_html
    assert "Body of the high scoring item" in item.summary_html


# --------------------------------------------------------------------------
# Discovery


def test_only_items_at_or_above_min_score_are_kept():
    item = build(min_score=8.0).discover()[0]
    assert "High scoring item" in item.body_html
    assert "Paper item" in item.body_html
    assert "Low scoring item" not in item.body_html
    assert item.extra == {"score_threshold": 8.0, "items_selected": 2, "items_available": 3}


def test_newest_digest_wins():
    item = build(min_score=8.0).discover()[0]
    assert item.id == "https://example.org/Horizon/2026/07/29/summary-zh.html"
    assert item.title == "Horizon 每日简报 2026-07-29"
    assert item.published_at.strftime("%Y-%m-%d") == "2026-07-29"


def test_articles_carry_their_payload_inline():
    item = build().discover()[0]
    assert item.fetch_strategy == FETCH_NONE
    assert item.download_url is None


def test_max_entries_caps_the_article_length():
    item = build(min_score=7.0, max_entries=1).discover()[0]
    assert item.extra["items_selected"] == 1
    assert "High scoring item" in item.body_html


def test_category_allowlist():
    item = build(min_score=7.0, categories=["papers"]).discover()[0]
    assert "Paper item" in item.body_html
    assert "High scoring item" not in item.body_html


def test_no_item_clears_the_threshold_yields_nothing():
    assert build(min_score=9.5).discover() == []


def test_body_stays_inside_the_tag_set_acfun_keeps():
    body = build(min_score=7.0, include_references=True).discover()[0].body_html
    assert tags_in(body) <= ALLOWED_TAGS
    assert not re.search(r"<a\b|href=|data-score|<details|<summary", body)


def test_source_links_survive_as_plain_urls():
    body = build(min_score=8.0).discover()[0].body_html
    assert "https://example.com/high" in body


def test_references_are_excluded_by_default():
    assert "ref.example.com" not in build(min_score=8.0).discover()[0].body_html


def test_context_can_be_dropped():
    body = build(min_score=8.0, include_context=False).discover()[0].body_html
    assert "Context for the high item" not in body


def test_each_category_heading_is_rendered_once():
    body = build(min_score=7.0).discover()[0].body_html
    # Horizon prefixes its section names with emoji; AcFun deletes those.
    assert body.count("<h2>科技 / AI</h2>") == 1
    # Grouping by category, not score, keeps the low-scoring tech item next to
    # the high-scoring one instead of after the papers section.
    assert body.index("Low scoring item") < body.index("论文精选")


# --------------------------------------------------------------------------
# Guard rails


def test_licence_must_be_stated_explicitly():
    # A Horizon digest summarises third-party reporting; MediaBridge must not
    # invent a licence for it.
    source = HorizonSource("horizon-daily", {}, session=_FakeSession())
    with pytest.raises(SourceError, match="license_name"):
        source.discover()


def test_feed_url_follows_the_language():
    assert build(lang="en").feed_url.endswith("/feed-en.xml")
    assert build(lang="zh").feed_url.endswith("/feed-zh.xml")


def test_explicit_feed_url_wins_over_language():
    source = build(feed_url="https://self.hosted/feed.xml", lang="en")
    assert source.feed_url == "https://self.hosted/feed.xml"
    source.discover()
    assert source.session.requested == ["https://self.hosted/feed.xml"]


def test_unknown_language_is_rejected():
    with pytest.raises(SourceError, match="unsupported lang"):
        build(lang="fr").discover()


def test_malformed_feed_reports_the_url():
    with pytest.raises(SourceError, match="not valid Atom"):
        build(session=_FakeSession("<feed><unclosed>")).discover()


def test_titles_with_markup_characters_are_escaped():
    # Item titles reach us as decoded text; re-embedding "R&D <ai>" raw would
    # have the flattener reparse "<ai>" as a tag and drop it.
    digest = DIGEST_HTML.replace("High scoring item", "R&amp;D &lt;ai&gt; wins")
    feed = FEED_XML.replace(DIGEST_HTML, digest)
    body = build(min_score=8.0, session=_FakeSession(feed)).discover()[0].body_html
    assert "R&amp;D &lt;ai&gt; wins" in body
    assert "<ai>" not in body


def test_min_score_below_horizons_own_floor_warns(caplog):
    with caplog.at_level("WARNING"):
        build(min_score=3.0).discover()
    assert "publication floor" in caplog.text
