"""AcFun article submission (专栏投稿).

No open-source implementation of this endpoint existed; the payload below was
recovered from AcFun's own ``post-article`` webpack chunk.

``POST /article/api/postArticle`` (form-urlencoded)::

    title          稿件标题
    description    简介, at most 200 characters (result=110014 above that)
    detail         JSON: {"bodyList": [{"orderId": 1, "title": ..., "txt": ...}]}
                   where txt is encodeURIComponent(<body html>)
    tagNames       JSON array of tag strings
    creationType   1 = 转载, 3 = 原创
    cover          cover image URL from the image upload flow
    channelId      top-level partition
    realmId        second-level realm
    supportZtEmot  true

Unlike video submission there is no ``originalLinkUrl`` field, so attribution
for a repost has to live in ``description``.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote

from ...errors import PublishError, SkipItem
from ...models import FetchedMedia, PublishResult
from ...utils.text import (
    MAX_ARTICLE_DESC_LEN,
    MAX_TITLE_LEN,
    collapse_whitespace,
    normalise_tags,
    render_template,
    render_within,
    truncate,
)
from ..base import Publisher
from .client import POST_ARTICLE_REFERER
from .video import upload_cover

log = logging.getLogger(__name__)

ARTICLE_COVER_BIZ_FLAG = "web-article-cover"

# JavaScript's encodeURIComponent leaves exactly these unescaped.
_JS_URI_SAFE = "-_.!~*'()"


def js_encode_uri_component(text: str) -> str:
    return quote(text, safe=_JS_URI_SAFE, encoding="utf-8")


class AcFunArticlePublisher(Publisher):
    """Publishes `MediaItem.body_html` as an AcFun 专栏 article."""

    type_name = "acfun_article"
    requires_media_file = False

    def validate_config(self, publish_config) -> None:
        if not publish_config.channel_id:
            raise PublishError(
                "publish.channel_id is required for acfun_article.",
                hint="Run `mediabridge channels --articles` to list realms.",
            )
        if not publish_config.realm_id:
            raise PublishError(
                "publish.realm_id is required for acfun_article.",
                hint="Run `mediabridge channels --articles` to list realms.",
            )

    def _render(self, item, publish_config) -> tuple[str, str, list[str]]:
        values = {
            "title": item.title,
            "description": item.description,
            "author": item.author or "未知",
            "webpage_url": item.webpage_url,
            "license": item.license or "见原始链接",
            "license_url": item.license_url,
            "source_name": item.source_name,
        }
        title = truncate(
            collapse_whitespace(render_template(publish_config.title_template, values)),
            MAX_TITLE_LEN,
        )
        description = render_within(publish_config.desc_template, values, MAX_ARTICLE_DESC_LEN)
        tags = normalise_tags([*publish_config.tags, *item.tags])
        return title, description, tags

    def _payload(self, item, publish_config, title: str, description: str, tags, cover_url: str) -> dict:
        body_list = [
            {
                "orderId": 1,
                "title": "",
                "txt": js_encode_uri_component(item.body_html),
            }
        ]
        return {
            "title": title,
            "description": description,
            "detail": json.dumps({"bodyList": body_list}, ensure_ascii=False),
            "tagNames": json.dumps(tags, ensure_ascii=False),
            "creationType": str(publish_config.creation_type),
            "cover": cover_url,
            "channelId": str(publish_config.channel_id),
            "realmId": str(publish_config.realm_id),
            "supportZtEmot": "true",
        }

    def _prepare(self, fetched: FetchedMedia, publish_config) -> tuple[str, str, list[str]]:
        self.validate_config(publish_config)
        if not fetched.item.body_html.strip():
            raise SkipItem(f"Article body is empty for {fetched.item.webpage_url}")
        return self._render(fetched.item, publish_config)

    def publish(self, fetched: FetchedMedia, publish_config) -> PublishResult:
        item = fetched.item
        title, description, tags = self._prepare(fetched, publish_config)

        if self.ctx.dry_run:
            log.info(
                "[dry-run] would post article to channel %s/realm %s: %s (%d chars of HTML)",
                publish_config.channel_id,
                publish_config.realm_id,
                title,
                len(item.body_html),
            )
            return PublishResult(ok=True, dry_run=True, message="dry-run", url=item.webpage_url)

        client = self.ctx.acfun()

        cover_url = ""
        if fetched.cover_path and fetched.cover_path.is_file():
            cover_url = upload_cover(client, fetched.cover_path, biz_flag=ARTICLE_COVER_BIZ_FLAG)

        payload = self._payload(item, publish_config, title, description, tags, cover_url)
        result = client.post_form("/article/api/postArticle", payload, referer=POST_ARTICLE_REFERER)
        article_id = result.get("articleId")
        if not article_id:
            raise PublishError(f"postArticle succeeded but returned no articleId: {result}")

        url = f"https://www.acfun.cn/a/ac{article_id}"
        log.info("Published article: %s -> %s", title, url)
        return PublishResult(ok=True, remote_id=str(article_id), url=url, message="submitted for review")

    def republish(self, fetched: FetchedMedia, publish_config, remote_id: str) -> PublishResult:
        """Re-submit an existing article (``updateArticle``, AcFun's 重新送审).

        The payload is identical to `publish` plus ``articleId``.
        """
        item = fetched.item
        title, description, tags = self._prepare(fetched, publish_config)

        if self.ctx.dry_run:
            log.info(
                "[dry-run] would update ac%s: %s (%d chars of HTML)",
                remote_id,
                title,
                len(item.body_html),
            )
            return PublishResult(ok=True, dry_run=True, message="dry-run", remote_id=remote_id)

        client = self.ctx.acfun()

        if fetched.cover_path and fetched.cover_path.is_file():
            cover_url = upload_cover(client, fetched.cover_path, biz_flag=ARTICLE_COVER_BIZ_FLAG)
        else:
            # An empty `cover` would clear the one already on the article, so a
            # failed cover download must not silently strip it.
            cover_url = get_article_cover(client, remote_id)
            if cover_url:
                log.info("Reusing the cover already on ac%s", remote_id)

        payload = self._payload(item, publish_config, title, description, tags, cover_url)
        payload["articleId"] = str(remote_id)

        client.post_form("/article/api/updateArticle", payload, referer=POST_ARTICLE_REFERER)
        url = f"https://www.acfun.cn/a/ac{remote_id}"
        log.info("Updated article: %s -> %s", title, url)
        return PublishResult(ok=True, remote_id=str(remote_id), url=url, message="resubmitted for review")


def get_article_cover(client, article_id: str) -> str:
    """Return the cover currently set on an article, or an empty string."""
    try:
        info = client.post_form(
            "/article/api/getArticleInfo", {"articleId": str(article_id)}, referer=POST_ARTICLE_REFERER
        )
    except PublishError as exc:
        log.warning("Could not read the existing cover for ac%s: %s", article_id, exc)
        return ""
    data = info.get("data") if isinstance(info.get("data"), dict) else info
    return str(data.get("titleImg") or "")
