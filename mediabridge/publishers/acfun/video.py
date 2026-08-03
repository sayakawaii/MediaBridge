"""AcFun video submission (投稿).

Pipeline, as performed by AcFun's own web client:

1. ``/video/api/getKSCloudToken``  -> taskId + upload token + chunk config
2. fragment upload to ``upload.kuaishouzt.com`` -> ``/api/upload/complete``
3. ``/video/api/createVideo``      -> videoId
4. cover: ``/common/api/getQiniuToken`` -> fragments -> ``/common/api/getUrlAfterUpload``
5. ``/video/api/createDouga``      -> dougaId

``/video/api/uploadFinish`` is intentionally absent from the success path. The
web client only calls it from ``catch`` blocks, passing ``errorCode`` /
``errorMsg``; it is failure telemetry, not a completion step.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ...errors import PublishError, SkipItem
from ...models import FetchedMedia, PublishResult
from ...utils.text import (
    MAX_DESC_LEN,
    MAX_TITLE_LEN,
    collapse_whitespace,
    normalise_tags,
    render_template,
    render_within,
    truncate,
)
from ..base import Publisher
from .client import UPLOAD_VIDEO_REFERER, AcFunClient
from .upload import DEFAULT_PART_SIZE, upload_file

log = logging.getLogger(__name__)

COVER_BIZ_FLAG = "web-douga-cover"


def upload_cover(client: AcFunClient, cover_path: Path, biz_flag: str = COVER_BIZ_FLAG) -> str:
    """Upload a cover image and return the URL AcFun assigns to it.

    ``getQiniuToken`` is a legacy name: the response points at Kuaishou, and
    the bytes travel over the same fragment protocol as the video itself.
    """
    token_response = client.post_json(
        "/common/api/getQiniuToken", {"fileName": cover_path.name}, referer=UPLOAD_VIDEO_REFERER
    )
    info = token_response.get("info") or {}
    upload_token = info.get("token")
    if not upload_token:
        raise PublishError(f"AcFun did not return a cover upload token: {token_response}")

    endpoints = info.get("httpEndpointList") or []
    host = endpoints[0] if endpoints else "upload.kuaishouzt.com"

    upload_file(
        client.session,
        cover_path,
        upload_token,
        host=host,
        part_size=DEFAULT_PART_SIZE,
        parallel=1,
        timeout=client.timeout,
        label="cover",
    )

    result = client.post_json(
        "/common/api/getUrlAfterUpload",
        {"token": upload_token, "bizFlag": biz_flag},
        referer=UPLOAD_VIDEO_REFERER,
    )
    url = result.get("url")
    if not url:
        raise PublishError(f"AcFun did not return a cover URL: {result}")
    return url


def upload_video_file(client: AcFunClient, video_path: Path) -> str:
    """Upload the media file and return the ``videoId`` AcFun assigns."""
    size = video_path.stat().st_size
    token_response = client.post_form(
        "/video/api/getKSCloudToken",
        {"fileName": video_path.name, "size": str(size), "template": "1"},
    )
    task_id = token_response.get("taskId")
    upload_token = token_response.get("token")
    if not task_id or not upload_token:
        raise PublishError(f"AcFun did not return an upload token: {token_response}")

    upload_config = token_response.get("uploadConfig") or {}
    part_size = int(upload_config.get("partSize") or DEFAULT_PART_SIZE)
    parallel = int(upload_config.get("parallel") or 2)

    try:
        upload_file(
            client.session,
            video_path,
            upload_token,
            part_size=part_size,
            parallel=parallel,
            timeout=client.upload_timeout,
            label="video",
        )
    except Exception as exc:
        _report_failure(client, task_id, exc)
        raise

    created = client.post_form(
        "/video/api/createVideo",
        {"videoKey": task_id, "fileName": video_path.name, "vodType": "ksCloud"},
    )
    video_id = created.get("videoId")
    if not video_id:
        raise PublishError(f"AcFun did not return a videoId: {created}")
    return str(video_id)


#: ``videoList[].sourceStatus`` in ``getDougaInfo``, per the creation centre's
#: own bundle. Transcoding runs after submission, so a douga id in hand says
#: nothing about whether the video will ever play.
SOURCE_STATUS = {1: "转码中", 2: "转码失败", 3: "审核中", 4: "已退回", 5: "已上线", 6: "时长超限"}
TRANSCODE_FAILED = 2

VERIFY_TIMEOUT_SEC = 240
VERIFY_INTERVAL_SEC = 15


def verify_transcode(client: AcFunClient, douga_id: str) -> str:
    """Watch a fresh submission until it leaves the pre-transcode state.

    ``createDouga`` returns an id for a video AcFun cannot decode just as
    readily as for one it can, so without this a broken upload looks exactly
    like a good one until somebody opens the page days later.
    """
    deadline = time.monotonic() + VERIFY_TIMEOUT_SEC
    status = None
    while time.monotonic() < deadline:
        try:
            info = client.post_form("/video/api/getDougaInfo", {"dougaId": str(douga_id)})
        except PublishError as exc:  # a lost poll must not fail a good submission
            log.debug("Could not read back ac%s: %s", douga_id, exc)
            return "unknown"

        videos = info.get("videoList") or []
        status = videos[0].get("sourceStatus") if videos else None
        # 2 is also where a submission sits before transcoding picks it up, so
        # only a lasting 2 means failure -- hence waiting rather than sampling.
        if status is not None and status != TRANSCODE_FAILED:
            log.info("ac%s transcoded: %s", douga_id, SOURCE_STATUS.get(status, status))
            return SOURCE_STATUS.get(status, str(status))
        time.sleep(VERIFY_INTERVAL_SEC)

    log.error(
        "ac%s is still '%s' after %ds. The submission exists but the video will not play; "
        "check the upload before trusting later runs.",
        douga_id,
        SOURCE_STATUS.get(status, status),
        VERIFY_TIMEOUT_SEC,
    )
    return SOURCE_STATUS.get(status, str(status))


def _report_failure(client: AcFunClient, task_id: str, exc: Exception) -> None:
    """Best-effort failure telemetry so AcFun can release the pending task."""
    try:
        client.post_form(
            "/video/api/uploadFinish",
            {"taskId": task_id, "errorCode": type(exc).__name__, "errorMsg": str(exc)[:200]},
        )
    except Exception:  # noqa: BLE001 - reporting a failure must never mask it
        log.debug("Could not report upload failure for task %s", task_id, exc_info=True)


class AcFunVideoPublisher(Publisher):
    """Publishes a downloaded video as an AcFun 稿件."""

    type_name = "acfun_video"

    def validate_config(self, publish_config) -> None:
        if not publish_config.channel_id:
            raise PublishError(
                "publish.channel_id is required for acfun_video.",
                hint="Run `mediabridge channels` to list valid partition IDs.",
            )

    def _render_fields(self, fetched: FetchedMedia, publish_config) -> dict[str, str]:
        item = fetched.item
        values = {
            "title": item.title,
            "description": item.description,
            "author": item.author or "未知",
            "webpage_url": item.webpage_url,
            "license": item.license or "见原始链接",
            "license_url": item.license_url,
            "source_name": item.source_name,
            "duration": item.duration or "",
        }
        title = truncate(
            collapse_whitespace(render_template(publish_config.title_template, values)),
            MAX_TITLE_LEN,
        )
        description = render_within(publish_config.desc_template, values, MAX_DESC_LEN)
        if not title:
            raise PublishError(f"Rendered an empty title for {item.webpage_url}")
        return {"title": title, "description": description}

    def publish(self, fetched: FetchedMedia, publish_config) -> PublishResult:
        self.validate_config(publish_config)
        item = fetched.item
        fields = self._render_fields(fetched, publish_config)
        tags = normalise_tags([*publish_config.tags, *item.tags])

        if self.ctx.dry_run:
            log.info(
                "[dry-run] would publish to channel %s: %s (%s)",
                publish_config.channel_id,
                fields["title"],
                item.webpage_url,
            )
            log.debug("[dry-run] description:\n%s\n[dry-run] tags: %s", fields["description"], tags)
            return PublishResult(ok=True, dry_run=True, message="dry-run", url=item.webpage_url)

        if not fetched.video_path or not fetched.video_path.is_file():
            raise SkipItem(f"No downloaded media file for {item.webpage_url}")
        if not fetched.cover_path or not fetched.cover_path.is_file():
            raise SkipItem(f"No cover image for {item.webpage_url}; AcFun requires one.")

        client = self.ctx.acfun()

        video_id = upload_video_file(client, fetched.video_path)
        log.info("Video registered as videoId=%s", video_id)

        cover_url = upload_cover(client, fetched.cover_path)
        log.info("Cover uploaded")

        payload = {
            "title": fields["title"],
            "description": fields["description"],
            "fansOnlyDesc": "",
            "tagNames": json.dumps(tags, ensure_ascii=False),
            "creationType": str(publish_config.creation_type),
            "channelId": str(publish_config.channel_id),
            "coverUrl": cover_url,
            "videoInfos": json.dumps([{"videoId": video_id, "title": fields["title"]}], ensure_ascii=False),
            "isJoinUpCollege": "0",
            # The web client only enables Kuaishou cross-posting for original
            # works; a repost must never be synced.
            "isSyncKs": "false",
            "originalDeclare": "1" if publish_config.original_declare else "0",
        }
        if publish_config.creation_type == 1:
            payload["originalLinkUrl"] = item.webpage_url

        result = client.post_form("/video/api/createDouga", payload)
        douga_id = result.get("dougaId")
        if not douga_id:
            raise PublishError(f"createDouga succeeded but returned no dougaId: {result}")

        url = f"https://www.acfun.cn/v/ac{douga_id}"
        log.info("Published: %s -> %s", fields["title"], url)
        outcome = verify_transcode(client, str(douga_id))
        return PublishResult(
            ok=True, remote_id=str(douga_id), url=url, message=f"submitted for review ({outcome})"
        )
