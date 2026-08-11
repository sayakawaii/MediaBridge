"""Where a repost's credit lives, and whether the 简介 still has room for it.

`postArticle` caps the 简介 at 200 characters, and none of the article sources
credit anybody: they hand over the upstream text and nothing else. So the
publisher appends the credit to the body, which `postArticle` does not limit.

The second half of this module guards the arithmetic that made that necessary.
A `desc_template` carrying `{webpage_url}` is one longer slug away from failing:
a 136-character `science.nasa.gov` URL once rendered a 176-character
attribution against the 200-character ceiling, and nothing would have noticed
the remaining 24 characters until a live run raised `ConfigError` and the item
stopped publishing. Rendering the shipped templates against a URL that has
already grown turns that into a test failure instead.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import pytest

from mediabridge.config import load_config
from mediabridge.models import FETCH_NONE, FetchedMedia, MediaItem
from mediabridge.publishers.acfun.article import (
    AcFunArticlePublisher,
    body_with_attribution,
    build_body_attribution,
)
from mediabridge.utils.text import (
    MAX_ARTICLE_DESC_LEN,
    MAX_VIDEO_DESC_LEN,
    collapse_whitespace,
    render_template,
    render_within,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILES = ("config.yaml", "config.example.yaml")

#: 160 characters. The real worst case measured against live data was 136, and
#: feeds build slugs out of headlines, so a longer one is a matter of time.
STRESS_URL = (
    "https://science.nasa.gov/learning-resources/science-activation/2026/08/12/"
    "community-college-instructors-are-bringing-an-astronomy-textbook-into-the-21st-century/"
)

#: `feed` falls back to the feed's own title when no author is configured, and
#: those run long -- "UN News - Global perspective Human stories" is 42.
STRESS_AUTHOR = "U.S. Geological Survey Earthquake Hazards Program"

#: The longest licence string in the tree today (`sources/nasa.py`).
STRESS_LICENSE = "NASA Media Usage (public domain, attribution requested)"

STRESS_VALUES = {
    "title": "Community college instructors bring an astronomy textbook into the 21st century",
    "description": "长" * 400,
    "author": STRESS_AUTHOR,
    "webpage_url": STRESS_URL,
    "license": STRESS_LICENSE,
    "license_url": "https://www.nasa.gov/nasa-brand-center/images-and-media/",
    "source_name": "nasa-news",
}

#: How much of the 200 a template must still leave for the upstream summary.
#: A description that cannot fit a sentence of the original is boilerplate with
#: a headline attached, and the room is the early warning: it shrinks quietly
#: as fixed text is added, long before anything actually fails.
MIN_SUMMARY_ROOM = 60


class _FakeClient:
    def __init__(self, responses=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}

    def post_form(self, path, data, referer=None):
        self.calls.append((path, data))
        return self.responses.get(path, {})

    def payload_for(self, path: str) -> dict:
        return next(data for called, data in self.calls if called == path)


class _Ctx:
    def __init__(self, client, dry_run: bool = False) -> None:
        self._client = client
        self.dry_run = dry_run

    def acfun(self):
        return self._client


class _Publish:
    target = "acfun_article"
    channel_id = 110
    realm_id = 28
    creation_type = 1
    tags: list[str] = ["科普"]
    title_template = "{title}"
    desc_template = "{description}\n\n转载｜原作者：{author}\n原始出处与许可见正文末尾。"


def _item(**kwargs) -> MediaItem:
    defaults = {
        "source_name": "nasa-news",
        "source_type": "feed",
        "id": "nasa-1",
        "title": "Community college instructors bring an astronomy textbook into the 21st century",
        "webpage_url": STRESS_URL,
        "description": "A short upstream summary.",
        "author": "NASA",
        "license": "NASA 内容原则上属公有领域，第三方素材除外",
        "license_url": "https://www.nasa.gov/nasa-brand-center/images-and-media/",
        "body_html": "<p>正文</p>",
        "fetch_strategy": FETCH_NONE,
    }
    return MediaItem(**{**defaults, **kwargs})


def _submitted_body(client: _FakeClient, path: str) -> str:
    detail = client.payload_for(path)["detail"]
    return unquote(detail.split('"txt": "', 1)[1].rsplit('"', 1)[0])


# --------------------------------------------------------------------------
# The credit in the body
# --------------------------------------------------------------------------


def test_the_body_credit_names_everything_the_simplified_summary_dropped():
    block = build_body_attribution(_item())
    assert "转载" in block
    assert "NASA" in block
    assert STRESS_URL in block
    assert "NASA 内容原则上属公有领域，第三方素材除外" in block
    assert "https://www.nasa.gov/nasa-brand-center/images-and-media/" in block


def test_the_body_credit_writes_urls_as_visible_text():
    # AcFun's sanitiser deletes every <a>, so a hyperlinked credit reaches the
    # reader with the URL gone. See mediabridge/utils/acfun_html.py.
    block = build_body_attribution(_item())
    assert "<a " not in block and "href" not in block


def test_the_credit_follows_whatever_the_source_produced():
    item = _item(body_html="<p>正文</p>")
    assert body_with_attribution(item).startswith("<p>正文</p>")


def test_an_unknown_author_is_labelled_rather_than_left_blank():
    assert "原作者：未知" in build_body_attribution(_item(author=""))


def test_a_missing_licence_url_leaves_no_empty_line():
    block = build_body_attribution(_item(license_url=""))
    assert "许可说明" not in block
    assert "<p></p>" not in block


def test_a_posted_article_carries_the_credit():
    client = _FakeClient({"/article/api/postArticle": {"articleId": 48762123}})
    AcFunArticlePublisher(_Ctx(client)).publish(FetchedMedia(item=_item()), _Publish())

    body = _submitted_body(client, "/article/api/postArticle")
    assert body.startswith("<p>正文</p>")
    assert STRESS_URL in body


def test_a_resubmitted_article_carries_the_credit_too():
    # `mediabridge refresh` is how already-published articles pick this up.
    client = _FakeClient({"/article/api/getArticleInfo": {"titleImg": "https://cdn/x.jpg"}})
    AcFunArticlePublisher(_Ctx(client)).republish(FetchedMedia(item=_item()), _Publish(), "48762123")

    assert STRESS_URL in _submitted_body(client, "/article/api/updateArticle")


def test_the_summary_no_longer_has_to_carry_the_url():
    description = AcFunArticlePublisher(_Ctx(_FakeClient()))._render(_item(), _Publish())[1]
    assert len(description) <= MAX_ARTICLE_DESC_LEN
    assert "转载" in description and "NASA" in description


# --------------------------------------------------------------------------
# Headroom in the shipped templates
# --------------------------------------------------------------------------


def _shipped_templates():
    for file_name in CONFIG_FILES:
        for source in load_config(REPO_ROOT / file_name).sources:
            yield pytest.param(
                source.publish.desc_template,
                source.publish.target,
                id=f"{file_name}:{source.name}",
            )


SHIPPED_TEMPLATES = list(_shipped_templates())


def _limit_for(target: str) -> int:
    return MAX_ARTICLE_DESC_LEN if target == "acfun_article" else MAX_VIDEO_DESC_LEN


@pytest.mark.parametrize(("template", "target"), SHIPPED_TEMPLATES)
def test_a_grown_url_still_leaves_room_for_a_summary(template, target):
    attribution = collapse_whitespace(render_template(template, {**STRESS_VALUES, "description": ""}))
    room = _limit_for(target) - len(attribution)
    assert room >= MIN_SUMMARY_ROOM, (
        f"the attribution alone renders to {len(attribution)} characters, leaving {room} of the "
        f"{_limit_for(target)} for the upstream summary. Shorten the fixed text in desc_template, "
        "or move what is too long into the article body, which has no limit."
    )


@pytest.mark.parametrize(("template", "target"), SHIPPED_TEMPLATES)
def test_a_grown_url_never_costs_a_shipped_source_its_publication(template, target):
    # `render_within` raises ConfigError rather than post an uncredited repost,
    # which for a scheduled run means the item silently stops publishing.
    limit = _limit_for(target)
    assert len(render_within(template, STRESS_VALUES, limit)) <= limit


@pytest.mark.parametrize(("template", "target"), SHIPPED_TEMPLATES)
def test_a_reader_who_only_sees_the_summary_still_knows_it_is_a_repost(template, target):
    rendered = render_within(template, STRESS_VALUES, _limit_for(target))
    assert "转载" in rendered
    assert STRESS_AUTHOR in rendered


def test_every_config_in_the_tree_is_covered():
    # A new config file added beside these would otherwise go unchecked.
    assert {path.name for path in REPO_ROOT.glob("config*.yaml")} == set(CONFIG_FILES)
