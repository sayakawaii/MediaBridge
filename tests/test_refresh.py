"""Re-submission of already-published items (`mediabridge refresh`)."""

from __future__ import annotations

import pytest

from mediabridge.errors import PublishError
from mediabridge.models import FETCH_NONE, FetchedMedia, MediaItem, PublishResult
from mediabridge.publishers.acfun.article import AcFunArticlePublisher
from mediabridge.publishers.acfun.video import AcFunVideoPublisher
from mediabridge.state import State


class _FakeClient:
    def __init__(self, responses=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}

    def post_form(self, path, data, referer=None):
        self.calls.append((path, data))
        return self.responses.get(path, {})

    def path_of(self, index: int) -> str:
        return self.calls[index][0]


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
    tags: list[str] = ["科技"]
    title_template = "{title}"
    desc_template = "{description}\n原始链接：{webpage_url}"


def _item() -> MediaItem:
    return MediaItem(
        source_name="horizon-daily",
        source_type="horizon",
        id="digest-1",
        title="Horizon 每日简报",
        webpage_url="https://example.org/d1",
        description="摘要",
        body_html="<p>正文</p>",
        fetch_strategy=FETCH_NONE,
    )


def test_update_carries_the_article_id_to_the_update_endpoint():
    client = _FakeClient({"/article/api/getArticleInfo": {"titleImg": "https://cdn/x.jpg"}})
    publisher = AcFunArticlePublisher(_Ctx(client))

    result = publisher.republish(FetchedMedia(item=_item()), _Publish(), "48736291")

    assert result.ok and result.remote_id == "48736291"
    assert result.url == "https://www.acfun.cn/a/ac48736291"
    path, payload = client.calls[-1]
    assert path == "/article/api/updateArticle"
    assert payload["articleId"] == "48736291"
    assert payload["channelId"] == "110" and payload["realmId"] == "28"


def test_update_without_a_new_cover_keeps_the_existing_one():
    # Sending an empty `cover` would blank the article's cover, so a failed
    # cover download must not quietly strip it.
    client = _FakeClient({"/article/api/getArticleInfo": {"titleImg": "https://cdn/existing.jpg"}})
    AcFunArticlePublisher(_Ctx(client)).republish(FetchedMedia(item=_item()), _Publish(), "123")

    assert client.path_of(0) == "/article/api/getArticleInfo"
    assert client.calls[-1][1]["cover"] == "https://cdn/existing.jpg"


def test_update_tolerates_an_unreadable_existing_cover():
    class Failing(_FakeClient):
        def post_form(self, path, data, referer=None):
            if path == "/article/api/getArticleInfo":
                raise PublishError("nope")
            return super().post_form(path, data, referer)

    client = Failing()
    result = AcFunArticlePublisher(_Ctx(client)).republish(FetchedMedia(item=_item()), _Publish(), "123")
    assert result.ok
    assert client.calls[-1][1]["cover"] == ""


def test_dry_run_updates_nothing():
    client = _FakeClient()
    result = AcFunArticlePublisher(_Ctx(client, dry_run=True)).republish(
        FetchedMedia(item=_item()), _Publish(), "123"
    )
    assert result.dry_run and client.calls == []


def test_video_refuses_to_update_rather_than_posting_a_duplicate():
    with pytest.raises(PublishError, match="cannot update"):
        AcFunVideoPublisher(_Ctx(_FakeClient())).republish(FetchedMedia(item=_item()), _Publish(), "1")


def test_refresh_records_a_timestamp_without_moving_the_publish_date(tmp_path):
    state = State(tmp_path / "published.json")
    item = _item()
    state.record(item, PublishResult(ok=True, remote_id="48736291", url="https://acfun/a/ac48736291"))
    published_at = state.published[item.dedup_key]["published_at"]

    state.mark_refreshed(item.dedup_key)

    entry = state.published[item.dedup_key]
    assert entry["published_at"] == published_at
    assert entry["refreshed_at"]


def test_remote_id_lookup_is_empty_for_unknown_items(tmp_path):
    assert State(tmp_path / "s.json").remote_id("nope:1") == ""
