"""Video submission: the description limit and the transcode read-back.

Both exist because of scheduled runs that went wrong. Five `tilvids` videos in
a row were downloaded, uploaded in full, and only then rejected by
`createDouga` with `result=109015 投稿失败：简介不能超过200个字` -- about fifty
minutes of upload thrown away, because the length check is server-side and runs
last.

The read-back then went wrong in the opposite direction. It waited four minutes
for the submission to leave `sourceStatus` 2 and called anything still there a
transcode failure, but 2 is also where a submission queues, and three
half-hour 1080p60 videos were condemned in one morning for the crime of being
big. All three had transcoded by the time anyone looked. Verification now
happens on the following run, and the publisher makes no judgement at all.
"""

from __future__ import annotations

import pytest

from mediabridge.config import DEFAULT_DESC_TEMPLATE, Config
from mediabridge.errors import ConfigError, PublishError
from mediabridge.models import FetchedMedia, MediaItem, PublishResult
from mediabridge.orchestrator import MAX_CONSECUTIVE_FAILURES, Orchestrator
from mediabridge.publishers.acfun import video as video_module
from mediabridge.publishers.acfun.video import AcFunVideoPublisher
from mediabridge.publishers.base import (
    VERIFY_FAILED,
    VERIFY_OK,
    VERIFY_PENDING,
    VERIFY_REJECTED,
)
from mediabridge.utils.text import (
    MAX_VIDEO_DESC_LEN,
    collapse_whitespace,
    render_template,
    render_within,
)

#: An LWN summary of the kind that triggered the live failure.
LONG_DESCRIPTION = (
    "The kernel development community has been discussing the future of the "
    "scheduler for some time now, and the latest posting proposes a rather "
    "different approach to the problem of load balancing across heterogeneous "
    "cores, one that would require changes throughout the tree."
)

VALUES = {
    "description": LONG_DESCRIPTION,
    "author": "Linux Weekly News",
    "webpage_url": "https://tilvids.com/w/mR8kQ2xLpN4vT7wYs3Jd6H",
    "license": "CC BY-SA 4.0",
}


def attribution_for(values: dict) -> str:
    """The template rendered with no upstream summary at all."""
    return collapse_whitespace(render_template(DEFAULT_DESC_TEMPLATE, {**values, "description": ""}))


class _FakeClient:
    def __init__(self, responses=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}

    def post_form(self, path, data, referer=None):
        self.calls.append((path, data))
        response = self.responses.get(path, {})
        if isinstance(response, Exception):
            raise response
        return response

    def payload_for(self, path: str) -> dict:
        return next(data for called, data in self.calls if called == path)


class _Ctx:
    def __init__(self, client, dry_run: bool = False) -> None:
        self._client = client
        self.dry_run = dry_run

    def acfun(self):
        return self._client


class _Publish:
    target = "acfun_video"
    channel_id = 86
    realm_id = None
    creation_type = 1
    tags: list[str] = []
    title_template = "【搬运】{title}"
    desc_template = DEFAULT_DESC_TEMPLATE
    original_declare = False


def _item(description: str = LONG_DESCRIPTION) -> MediaItem:
    return MediaItem(
        source_name="tilvids",
        source_type="peertube",
        id="mR8kQ2xLpN4vT7wYs3Jd6H",
        title="A new approach to scheduler load balancing",
        webpage_url=VALUES["webpage_url"],
        description=description,
        author=VALUES["author"],
        license=VALUES["license"],
    )


def publish(monkeypatch, tmp_path, item, responses=None):
    """Publish `item` with the upload stages stubbed out, returning the client too."""
    monkeypatch.setattr(video_module, "upload_video_file", lambda client, path: "video-1")
    monkeypatch.setattr(video_module, "upload_cover", lambda client, path: "https://cdn.example/c.jpg")

    for name in ("video.mp4", "cover.jpg"):
        (tmp_path / name).write_bytes(b"\0")

    client = _FakeClient(
        {
            "/video/api/createDouga": {"dougaId": 48762123},
            "/video/api/getDougaInfo": {"videoList": [{"sourceStatus": 5}]},
            **(responses or {}),
        }
    )
    fetched = FetchedMedia(item=item, video_path=tmp_path / "video.mp4", cover_path=tmp_path / "cover.jpg")
    return AcFunVideoPublisher(_Ctx(client)).publish(fetched, _Publish()), client


def submit(monkeypatch, tmp_path, item, responses=None):
    """As `publish`, but returning the payload `createDouga` was sent."""
    result, client = publish(monkeypatch, tmp_path, item, responses)
    return result, client.payload_for("/video/api/createDouga")


# --------------------------------------------------------------------------
# The description limit
# --------------------------------------------------------------------------


def test_an_overlong_description_is_cut_before_anything_is_uploaded(monkeypatch, tmp_path):
    _, payload = submit(monkeypatch, tmp_path, _item())
    assert len(payload["description"]) <= MAX_VIDEO_DESC_LEN


def test_the_attribution_survives_the_cut(monkeypatch, tmp_path):
    # The whole point: the summary is expendable, the credit is not.
    _, payload = submit(monkeypatch, tmp_path, _item())
    assert payload["description"].endswith(attribution_for(VALUES))
    assert VALUES["webpage_url"] in payload["description"]
    assert VALUES["author"] in payload["description"]
    assert VALUES["license"] in payload["description"]


def test_the_shortened_summary_is_marked_as_shortened(monkeypatch, tmp_path):
    _, payload = submit(monkeypatch, tmp_path, _item())
    summary = payload["description"].split("――――――――――")[0]
    assert summary.startswith("The kernel development community")
    assert summary.rstrip().endswith("…")


def test_truncation_is_logged_with_the_original_length(monkeypatch, tmp_path, caplog):
    with caplog.at_level("WARNING"):
        submit(monkeypatch, tmp_path, _item())
    rendered_length = len(collapse_whitespace(render_template(DEFAULT_DESC_TEMPLATE, VALUES)))
    assert str(rendered_length) in caplog.text


def test_a_description_already_within_the_limit_is_left_alone(monkeypatch, tmp_path):
    short = "A short upstream summary."
    values = {**VALUES, "description": short}
    _, payload = submit(monkeypatch, tmp_path, _item(short))

    assert payload["description"] == collapse_whitespace(render_template(DEFAULT_DESC_TEMPLATE, values))
    assert "…" not in payload["description"]


def test_nothing_is_logged_when_the_description_already_fits(monkeypatch, tmp_path, caplog):
    with caplog.at_level("WARNING"):
        submit(monkeypatch, tmp_path, _item("A short upstream summary."))
    assert "shortening" not in caplog.text


def test_an_attribution_that_cannot_fit_is_an_error_rather_than_a_silent_trim():
    # No amount of shrinking the summary helps here, and trimming the end would
    # throw away exactly what makes the repost legitimate.
    values = {**VALUES, "author": "N" * 300}
    with pytest.raises(ConfigError, match="desc_template renders to"):
        render_within(DEFAULT_DESC_TEMPLATE, values, MAX_VIDEO_DESC_LEN)


def test_the_video_limit_matches_what_createdouga_enforces():
    # 109015 comes back with an empty error_msg about half the time, so the
    # limit has to be known here rather than parsed out of the rejection.
    assert MAX_VIDEO_DESC_LEN == 200


# --------------------------------------------------------------------------
# Transcode verification
#
# It no longer happens here. `createDouga` returning an id ends the publisher's
# involvement; what AcFun made of the file is read back by a later run, where
# "still queued" and "failed" are finally distinguishable.
# --------------------------------------------------------------------------


def test_an_accepted_submission_is_published_and_left_to_be_verified(monkeypatch, tmp_path):
    result, _ = submit(monkeypatch, tmp_path, _item())
    assert result.ok is True
    assert result.remote_id == "48762123"
    assert result.pending_verification is True


def test_publishing_does_not_poll_the_transcode_status(monkeypatch, tmp_path):
    # The four-minute wait this replaces cost twelve minutes of one run and
    # condemned three videos that had transcoded perfectly by the next morning.
    _result, client = publish(monkeypatch, tmp_path, _item())
    assert "/video/api/getDougaInfo" not in [path for path, _data in client.calls]


def verify_with(status, *, calls=None):
    """Ask the publisher what became of ac48762123, given a `sourceStatus`."""
    response = status if isinstance(status, (Exception, dict)) else {"videoList": [{"sourceStatus": status}]}
    client = _FakeClient({"/video/api/getDougaInfo": response})
    outcome = AcFunVideoPublisher(_Ctx(client)).verify("48762123")
    if calls is not None:
        calls.extend(client.calls)
    return outcome


@pytest.mark.parametrize(("status", "label"), [(3, "审核中"), (5, "已上线")])
def test_a_transcoded_submission_verifies(status, label):
    outcome = verify_with(status)
    assert outcome.state == VERIFY_OK
    assert outcome.detail == label


def test_a_lasting_transcode_failure_is_a_failure_once_it_is_asked_a_day_later():
    outcome = verify_with(2)
    assert outcome.state == VERIFY_FAILED
    assert outcome.detail == "转码失败"


@pytest.mark.parametrize("status", [4, 6])
def test_a_submission_acfun_refused_is_not_offered_for_retry(status):
    # 已退回 and 时长超限 are terminal, and re-uploading the same file would only
    # reproduce them, so these must not release the dedup key.
    assert verify_with(status).state == VERIFY_REJECTED


def test_a_status_still_in_progress_stays_unresolved():
    assert verify_with(1).state == VERIFY_PENDING


def test_a_status_this_build_does_not_know_stays_unresolved():
    # A code added to AcFun's enum later must not be read as a verdict.
    outcome = verify_with(99)
    assert outcome.state == VERIFY_PENDING
    assert "99" in outcome.detail


def test_an_empty_video_list_stays_unresolved():
    assert verify_with({"videoList": []}).state == VERIFY_PENDING


def test_an_unreadable_read_back_stays_unresolved():
    # The poll failing says nothing about the video; treating it as failure
    # would re-upload a file that is probably fine.
    outcome = verify_with(PublishError("gateway timeout"))
    assert outcome.state == VERIFY_PENDING
    assert "gateway timeout" in outcome.detail


# --------------------------------------------------------------------------
# What the orchestrator does with the result
# --------------------------------------------------------------------------


class _FakeSource:
    def __init__(self, name: str, count: int) -> None:
        self.name = name
        self.count = count
        self.limits = None

    def discover(self) -> list[MediaItem]:
        return [
            MediaItem(
                source_name=self.name,
                source_type="fake",
                id=f"item-{i}",
                title=f"item {i}",
                webpage_url=f"https://tilvids.example/{i}",
            )
            for i in range(self.count)
        ]


def orchestrate(monkeypatch, tmp_path, result: PublishResult, candidates: int = 1, **limits):
    """Run one source whose publisher always returns `result`."""
    attempts: list[str] = []

    class _Publisher:
        def validate_config(self, publish_config) -> None:
            pass

        def publish(self, fetched, publish_config) -> PublishResult:
            attempts.append(fetched.item.id)
            return result

    monkeypatch.setattr(
        "mediabridge.orchestrator.build_source",
        lambda name, type_name, options, session=None: _FakeSource(name, candidates),
    )
    monkeypatch.setattr("mediabridge.orchestrator.build_publisher", lambda target, ctx: _Publisher())

    limits.setdefault("max_items_per_source", 1)
    config = Config.model_validate(
        {
            "state_file": str(tmp_path / "published.json"),
            "work_dir": str(tmp_path / "work"),
            "limits": limits,
            "sources": [
                {"name": "tilvids", "type": "fake", "publish": {"target": "acfun_video", "channel_id": 86}}
            ],
        }
    )
    orchestrator = Orchestrator(config, dry_run=True)
    return orchestrator, orchestrator.run(), attempts


def test_a_failed_transcode_is_kept_out_of_the_ledger(monkeypatch, tmp_path):
    # Recording it would mean the dedup key is spent for good and a permanently
    # broken video sits on the account with nothing ever retrying it.
    failed = PublishResult(ok=False, remote_id="48762123", message="could not transcode it")
    orchestrator, report, _ = orchestrate(monkeypatch, tmp_path, failed)

    assert report.published == 0 and report.failed == 1
    assert orchestrator.state.published == {}
    assert not (tmp_path / "published.json").exists()


def test_an_unknown_read_back_is_still_recorded(monkeypatch, tmp_path):
    ok = PublishResult(ok=True, remote_id="48762123", message="submitted for review (unknown)")
    orchestrator, report, _ = orchestrate(monkeypatch, tmp_path, ok)

    assert report.published == 1
    assert orchestrator.state.published["tilvids:item-0"]["remote_id"] == "48762123"


def test_a_source_failing_over_and_over_is_abandoned(monkeypatch, tmp_path):
    # The live run attempted six videos from one source under
    # `max_items_per_source: 1`, because failures never count against it.
    failed = PublishResult(ok=False, message="投稿失败：简介不能超过200个字")
    _, report, attempts = orchestrate(monkeypatch, tmp_path, failed, candidates=6, max_items_per_run=1)

    assert len(attempts) == MAX_CONSECUTIVE_FAILURES
    assert report.failed == MAX_CONSECUTIVE_FAILURES


def test_the_failure_run_resets_after_something_publishes(monkeypatch, tmp_path):
    results = iter(
        [
            PublishResult(ok=False, message="boom"),
            PublishResult(ok=False, message="boom"),
            PublishResult(ok=True, remote_id="1"),
            PublishResult(ok=False, message="boom"),
            PublishResult(ok=False, message="boom"),
            PublishResult(ok=False, message="boom"),
        ]
    )

    attempts: list[str] = []

    class _Publisher:
        def validate_config(self, publish_config) -> None:
            pass

        def publish(self, fetched, publish_config) -> PublishResult:
            attempts.append(fetched.item.id)
            return next(results)

    monkeypatch.setattr(
        "mediabridge.orchestrator.build_source",
        lambda name, type_name, options, session=None: _FakeSource(name, 6),
    )
    monkeypatch.setattr("mediabridge.orchestrator.build_publisher", lambda target, ctx: _Publisher())

    config = Config.model_validate(
        {
            "state_file": str(tmp_path / "published.json"),
            "work_dir": str(tmp_path / "work"),
            "limits": {"max_items_per_source": 9, "max_items_per_run": 9},
            "sources": [
                {"name": "tilvids", "type": "fake", "publish": {"target": "acfun_video", "channel_id": 86}}
            ],
        }
    )
    report = Orchestrator(config, dry_run=True).run()

    # Two failures, a publish, then three more: all six are attempted.
    assert len(attempts) == 6
    assert report.published == 1 and report.failed == 5
