"""Generic feed source tests.

The fixtures mirror the two shapes seen in practice: UN News, whose RSS carries
a one-line summary and keeps the article on the page, and NASA, whose RSS
carries the whole article in ``content:encoded``.
"""

from __future__ import annotations

import pytest

from mediabridge.errors import SourceError
from mediabridge.models import FETCH_NONE
from mediabridge.sources.feed import FeedSource, extract_container
from tests.test_acfun_html import ALLOWED_TAGS, tags_in

SCRAPE_FEED = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>联合国新闻</title>
    <item>
      <title>某机构警告航运存在生态风险</title>
      <link>https://news.example.org/feed/view/zh/story/1</link>
      <guid>https://news.example.org/zh/story/1</guid>
      <description>该机构警告称，长期停泊的船只可能成为生态定时炸弹。</description>
      <pubDate>Wed, 05 Aug 2026 12:00:00 +0000</pubDate>
    </item>
    <item>
      <title>没有正文的条目</title>
      <link>https://news.example.org/feed/view/zh/story/2</link>
      <guid>https://news.example.org/zh/story/2</guid>
      <description>短摘要。</description>
      <pubDate>Wed, 05 Aug 2026 11:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

ARTICLE_PAGE = """
<html><body>
  <div class="header">导航栏，不应出现在正文里</div>
  <div class="clearfix text-formatted field--name-field-text-column field__item">
    <p>这一担忧主要集中在生物污损上，也就是藻类和小型海洋生物在船体表面逐渐积聚的现象。
       长期停泊而没有清理的船只面临的风险明显更高，一旦重新启航，附着的物种就可能被带往
       完全不同的海域。</p>
    <figure><img src="https://photos.example.org/tanker.jpg"><figcaption>图片说明</figcaption></figure>
    <div class="inner">嵌套层里的文字也属于正文，提取时不应当在这里被截断。</div>
    <h4>入侵物种的影响</h4>
    <p>该机构表示，随着航运和贸易活动增加，生物污损已经成为一个日益严重的全球性问题。
       在部分地区，入侵性水生物种造成的影响被形容为毁灭性的，因为这些物种一旦在新环境中
       定居下来就极难清除，治理成本往往远高于事前预防。</p>
  </div>
  <div class="footer">页脚，不应出现在正文里</div>
</body></html>
"""

STORY_TEXT = "A first of its kind measurement may have captured empty space behaving oddly. " * 4
APOD_TEXT = "Today's APOD Archive Submissions " * 20
ATOM_TEXT = "Atom bodies are supported as well, with enough text to clear the floor. " * 4

CONTENT_FEED = f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>NASA</title>
    <item>
      <title>A Real Science Story</title>
      <link>https://science.example.gov/story</link>
      <category>Missions</category>
      <description>A short teaser.</description>
      <pubDate>Wed, 05 Aug 2026 12:00:00 +0000</pubDate>
      <content:encoded><![CDATA[<p>{STORY_TEXT}</p><p>Second paragraph with detail.</p>]]></content:encoded>
    </item>
    <item>
      <title>APOD: Picture Of The Day</title>
      <link>https://science.example.gov/apod</link>
      <category>APOD</category>
      <description>Navigation soup.</description>
      <content:encoded><![CDATA[<p>{APOD_TEXT}</p>]]></content:encoded>
    </item>
  </channel>
</rss>
"""

ATOM_FEED = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Newsroom</title>
  <entry>
    <title>An Atom Entry</title>
    <link rel="alternate" href="https://atom.example.org/story"/>
    <id>tag:atom.example.org,2026:1</id>
    <updated>2026-08-05T12:00:00Z</updated>
    <summary>A summary line.</summary>
    <content type="html">&lt;p&gt;{ATOM_TEXT}&lt;/p&gt;</content>
  </entry>
</feed>
"""


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self.content = body.encode("utf-8")
        self.text = body
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        pass


class _FakeSession:
    """Answers each URL from `routes`, matched on substring."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        for fragment, body in self.routes.items():
            if fragment in url:
                return _FakeResponse(body)
        raise AssertionError(f"unexpected request to {url}")


def build_scrape(**options) -> FeedSource:
    options.setdefault("url", "https://news.example.org/rss.xml")
    options.setdefault("body", "scrape")
    options.setdefault("body_class", "field--name-field-text-column")
    options.setdefault("license_name", "允许全文转载")
    session = options.pop("session", None) or _FakeSession(
        {"rss.xml": SCRAPE_FEED, "/story/1": ARTICLE_PAGE, "/story/2": "<html><body></body></html>"}
    )
    return FeedSource("un-news", options, session=session)


def build_content(**options) -> FeedSource:
    options.setdefault("url", "https://science.example.gov/rss.xml")
    options.setdefault("license_name", "public domain")
    session = options.pop("session", None) or _FakeSession({"rss.xml": CONTENT_FEED})
    return FeedSource("nasa-news", options, session=session)


# --------------------------------------------------------------------------
# Container extraction
# --------------------------------------------------------------------------


def test_extract_container_spans_nested_divs():
    """A non-greedy regex would stop at the first `</div>` and lose the tail."""
    inner = extract_container(ARTICLE_PAGE, "field--name-field-text-column")
    assert "嵌套层里的文字也属于正文" in inner
    assert "该机构表示" in inner
    assert "导航栏" not in inner
    assert "页脚" not in inner


def test_extract_container_returns_empty_when_absent():
    assert extract_container(ARTICLE_PAGE, "no-such-class") == ""
    assert extract_container(ARTICLE_PAGE, "") == ""


def test_extract_container_tolerates_unbalanced_markup():
    html = '<div class="body"><p>text</p>'
    assert "text" in extract_container(html, "body")


def test_extract_container_ignores_comments():
    html = '<div class="body"><!-- <div>decoy</div> --><p>real</p></div><p>after</p>'
    inner = extract_container(html, "body")
    assert "real" in inner
    assert "after" not in inner


# --------------------------------------------------------------------------
# Configuration guards
# --------------------------------------------------------------------------


def test_license_name_is_mandatory():
    with pytest.raises(SourceError, match="license_name"):
        build_scrape(license_name="").discover()


def test_url_is_mandatory():
    with pytest.raises(SourceError, match="options.url"):
        build_scrape(url="").discover()


def test_scrape_requires_body_class():
    with pytest.raises(SourceError, match="body_class"):
        build_scrape(body_class="").discover()


def test_body_mode_is_validated():
    with pytest.raises(SourceError, match="'content' or 'scrape'"):
        build_scrape(body="magic").discover()


def test_unparseable_feed_is_reported():
    session = _FakeSession({"rss.xml": "<rss><unclosed>"})
    with pytest.raises(SourceError, match="not valid RSS or Atom"):
        build_scrape(session=session).discover()


# --------------------------------------------------------------------------
# Scrape mode
# --------------------------------------------------------------------------


def test_scrape_mode_builds_an_article_item():
    items = build_scrape(limit=1).discover()
    assert len(items) == 1
    item = items[0]
    assert item.title == "某机构警告航运存在生态风险"
    assert item.fetch_strategy == FETCH_NONE
    assert "生物污损" in item.body_html
    assert tags_in(item.body_html) <= ALLOWED_TAGS


def test_summary_is_prepended_as_the_lead():
    body = build_scrape(limit=1).discover()[0].body_html
    assert body.index("生态定时炸弹") < body.index("生物污损")


def test_summary_can_be_suppressed():
    body = build_scrape(limit=1, include_summary=False).discover()[0].body_html
    assert "生态定时炸弹" not in body


def test_strip_images_removes_photographs():
    """UN text may be reposted; UN photography may not."""
    with_images = build_scrape(limit=1).discover()[0].body_html
    assert "<img" in with_images

    without = build_scrape(limit=1, strip_images=True).discover()[0].body_html
    assert "<img" not in without
    assert "生物污损" in without


def test_prefer_guid_link_avoids_the_tracking_redirect():
    default = build_scrape(limit=1).discover()[0]
    assert "/feed/view/" in default.webpage_url

    canonical = build_scrape(limit=1, prefer_guid_link=True).discover()[0]
    assert canonical.webpage_url == "https://news.example.org/zh/story/1"


def test_entries_without_a_body_are_skipped():
    """The second fixture entry scrapes to nothing, which must not be published."""
    items = build_scrape(limit=5).discover()
    assert [item.title for item in items] == ["某机构警告航运存在生态风险"]


def test_author_falls_back_to_the_feed_title():
    assert build_scrape(limit=1).discover()[0].author == "联合国新闻"
    assert build_scrape(limit=1, author="联合国新闻中文网").discover()[0].author == "联合国新闻中文网"


# --------------------------------------------------------------------------
# Content mode
# --------------------------------------------------------------------------


def test_content_mode_uses_the_inline_article():
    item = build_content(limit=1).discover()[0]
    assert item.title == "A Real Science Story"
    assert "empty space behaving oddly" in item.body_html
    assert tags_in(item.body_html) <= ALLOWED_TAGS


def test_content_mode_makes_no_page_requests():
    session = _FakeSession({"rss.xml": CONTENT_FEED})
    build_content(limit=1, session=session).discover()
    assert session.requested == ["https://science.example.gov/rss.xml"]


def test_excluded_categories_are_dropped():
    titles = [item.title for item in build_content(limit=5, exclude_categories=["APOD"]).discover()]
    assert titles == ["A Real Science Story"]


def test_included_categories_act_as_an_allowlist():
    titles = [item.title for item in build_content(limit=5, include_categories=["apod"]).discover()]
    assert titles == ["APOD: Picture Of The Day"]


def test_limit_caps_the_result():
    assert len(build_content(limit=1).discover()) == 1


def test_min_body_chars_filters_thin_entries():
    assert build_content(limit=5, min_body_chars=100000).discover() == []


def test_max_body_chars_is_enforced():
    items = build_content(limit=5, max_body_chars=50).discover()
    assert items == []


def test_atom_feeds_are_supported():
    session = _FakeSession({"rss.xml": ATOM_FEED})
    item = build_content(limit=1, session=session).discover()[0]
    assert item.title == "An Atom Entry"
    assert item.webpage_url == "https://atom.example.org/story"
    assert "Atom bodies are supported" in item.body_html
