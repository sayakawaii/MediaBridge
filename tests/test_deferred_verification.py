"""Verifying a submission on the run *after* the one that made it.

The live failure this replaces: three tilvids videos were uploaded, accepted by
`createDouga`, and then polled for four minutes each. All three were still in
`sourceStatus` 2 at the deadline and were written off as transcode failures.
Hours later all three were at 3 (审核中) with full AcFun playback manifests --
1080P60 down to 360P, correct durations. 2 is where a submission queues as well
as where a failed one lands, and half an hour of 1080p60 queues for longer than
four minutes.

Costs of getting it wrong, both of which these tests pin down: the run threw
away forty minutes and reported nothing published, and because a failed publish
is deliberately kept out of the ledger, the next run would have re-downloaded,
re-uploaded and re-submitted all three as duplicates.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mediabridge.config import Config
from mediabridge.models import FetchedMedia, MediaItem, PublishResult
from mediabridge.orchestrator import VERIFY_ABANDONED, Orchestrator
from mediabridge.publishers.base import (
    VERIFY_FAILED,
    VERIFY_OK,
    VERIFY_PENDING,
    VERIFY_REJECTED,
    Verification,
)
from mediabridge.state import State

KEY = "tilvids:41c054e0-bf71-4b76-ac3f-387881f54d67"

#: The shape every entry committed before deferred verification existed has.
LEGACY_ENTRY = {
    "published_at": "2026-08-10T04:16:48+00:00",
    "remote_id": "48764093",
    "remote_url": "https://www.acfun.cn/v/ac48764093",
    "source_url": "https://tilvids.com/w/xAC6urYo6WR5wDzcxyzJVS",
    "title": "pantheon",
}


def _item(source_name: str = "tilvids", item_id: str = "41c054e0") -> MediaItem:
    return MediaItem(
        source_name=source_name,
        source_type="peertube",
        id=item_id,
        title="Linux Weekly News",
        webpage_url="https://tilvids.com/w/97Va9Pu7nXkRRX1ZiCGu1p",
    )


def _ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


def test_a_pending_entry_round_trips_through_the_ledger(tmp_path):
    path = tmp_path / "published.json"
    state = State(path).load()
    state.record(
        _item(),
        PublishResult(ok=True, remote_id="48766867", url="https://acfun/ac1", pending_verification=True),
    )
    state.save()

    reloaded = State(path).load()
    assert reloaded.is_published("tilvids:41c054e0")
    assert [key for key, _entry in reloaded.pending_verification()] == ["tilvids:41c054e0"]


def test_an_item_with_nothing_to_verify_carries_no_marker(tmp_path):
    # Articles are live the moment `postArticle` returns; marking them pending
    # would put them in a queue nothing ever resolves.
    state = State(tmp_path / "published.json").load()
    state.record(_item(), PublishResult(ok=True, remote_id="1"))

    assert "verification" not in state.published["tilvids:41c054e0"]
    assert state.pending_verification() == []


def test_entries_written_before_verification_existed_still_load(tmp_path):
    path = tmp_path / "published.json"
    path.write_text(
        json.dumps({"version": 1, "updated_at": _ago(days=1), "published": {KEY: LEGACY_ENTRY}}),
        encoding="utf-8",
    )
    state = State(path).load()

    assert state.is_published(KEY)
    assert state.remote_id(KEY) == "48764093"
    # No marker means there was never anything outstanding, so a run that has
    # just gained this feature must not go and re-check the back catalogue.
    assert state.pending_verification() == []


def test_resolving_a_verification_leaves_the_item_published(tmp_path):
    state = State(tmp_path / "published.json").load()
    state.record(_item(), PublishResult(ok=True, remote_id="1", pending_verification=True))
    state.resolve_verification("tilvids:41c054e0", VERIFY_OK)

    assert state.is_published("tilvids:41c054e0")
    assert state.published["tilvids:41c054e0"]["verification"] == VERIFY_OK
    assert state.published["tilvids:41c054e0"]["verified_at"]
    assert state.pending_verification() == []


def test_releasing_an_entry_frees_the_dedup_key(tmp_path):
    state = State(tmp_path / "published.json").load()
    state.record(_item(), PublishResult(ok=True, remote_id="1", pending_verification=True))

    assert state.release("tilvids:41c054e0")["remote_id"] == "1"
    assert not state.is_published("tilvids:41c054e0")
    assert state.release("tilvids:41c054e0") is None


# --------------------------------------------------------------------------
# What the orchestrator does with a pending entry
# --------------------------------------------------------------------------


class _FakeSource:
    """Yields the items it was given, so a released key can be seen retried."""

    def __init__(self, name: str, items: list[MediaItem]) -> None:
        self.name = name
        self.items = items
        self.limits = None

    def discover(self) -> list[MediaItem]:
        return list(self.items)


class _FakePublisher:
    def __init__(self, outcome: Verification | Exception) -> None:
        self.outcome = outcome
        self.asked: list[str] = []
        self.published: list[str] = []

    def validate_config(self, publish_config) -> None:
        pass

    def publish(self, fetched, publish_config) -> PublishResult:
        self.published.append(fetched.item.id)
        return PublishResult(ok=True, remote_id="99", url="https://acfun/ac99", pending_verification=True)

    def verify(self, remote_id: str) -> Verification:
        self.asked.append(remote_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def build(monkeypatch, tmp_path, entry, outcome, *, candidates=(), dry_run=False):
    """An orchestrator whose ledger holds one pending entry for `tilvids`."""
    path = tmp_path / "published.json"
    path.write_text(
        json.dumps({"version": 1, "updated_at": _ago(days=1), "published": {KEY: entry}}),
        encoding="utf-8",
    )

    publisher = _FakePublisher(outcome)
    monkeypatch.setattr(
        "mediabridge.orchestrator.build_source",
        lambda name, type_name, options, session=None: _FakeSource(name, list(candidates)),
    )
    monkeypatch.setattr("mediabridge.orchestrator.build_publisher", lambda target, ctx: publisher)
    monkeypatch.setattr("mediabridge.orchestrator.fetch", lambda item, work_dir, limits: FetchedMedia(item))

    config = Config.model_validate(
        {
            "state_file": str(path),
            "work_dir": str(tmp_path / "work"),
            "limits": {"max_items_per_run": 4, "max_items_per_source": 1},
            "sources": [
                {"name": "tilvids", "type": "fake", "publish": {"target": "acfun_video", "channel_id": 86}}
            ],
        }
    )
    orchestrator = Orchestrator(config, dry_run=dry_run)
    return orchestrator, publisher


def pending(days: float = 1, **overrides) -> dict:
    return {**LEGACY_ENTRY, "published_at": _ago(days=days), "verification": "pending", **overrides}


def test_a_transcoded_submission_is_marked_verified_and_left_alone(monkeypatch, tmp_path):
    orchestrator, publisher = build(monkeypatch, tmp_path, pending(), Verification(VERIFY_OK, "审核中"))
    report = orchestrator.run()

    assert publisher.asked == ["48764093"]
    assert orchestrator.state.published[KEY]["verification"] == VERIFY_OK
    assert report.verified == 1
    assert report.verification_failures == []


def test_a_submission_still_in_progress_is_not_condemned(monkeypatch, tmp_path):
    # The whole point. Anything short of a verdict leaves the entry pending, to
    # be asked about again tomorrow.
    orchestrator, _ = build(monkeypatch, tmp_path, pending(), Verification(VERIFY_PENDING, "转码中"))
    report = orchestrator.run()

    assert orchestrator.state.published[KEY]["verification"] == "pending"
    assert report.verification_failures == []
    assert report.verified == 0


def test_a_confirmed_failure_releases_the_dedup_key(monkeypatch, tmp_path):
    orchestrator, _ = build(monkeypatch, tmp_path, pending(), Verification(VERIFY_FAILED, "转码失败"))
    report = orchestrator.run()

    assert not orchestrator.state.is_published(KEY)
    assert len(report.verification_failures) == 1
    # The id has to be in there: nothing here can delete the dead submission.
    assert "ac48764093" in report.verification_failures[0]


def test_a_confirmed_failure_is_logged_with_the_id_to_delete(monkeypatch, tmp_path, caplog):
    orchestrator, _ = build(monkeypatch, tmp_path, pending(), Verification(VERIFY_FAILED, "转码失败"))
    with caplog.at_level("ERROR"):
        orchestrator.run()

    assert "Delete ac48764093 by hand" in caplog.text
    assert "https://www.acfun.cn/v/ac48764093" in caplog.text


def test_a_released_item_is_retried_by_the_same_run(monkeypatch, tmp_path):
    # Releasing the key is only worth anything if something picks the item up.
    retryable = _item(item_id="41c054e0-bf71-4b76-ac3f-387881f54d67")
    orchestrator, publisher = build(
        monkeypatch,
        tmp_path,
        pending(),
        Verification(VERIFY_FAILED, "转码失败"),
        candidates=[retryable],
    )
    report = orchestrator.run()

    assert publisher.published == ["41c054e0-bf71-4b76-ac3f-387881f54d67"]
    assert report.published == 1
    assert orchestrator.state.published[KEY]["verification"] == "pending"


def test_a_verification_failure_does_not_fail_the_run(monkeypatch, tmp_path):
    # It has already been retried automatically; the only outstanding job is a
    # human deleting the corpse, and a red run every day would not help.
    orchestrator, _ = build(monkeypatch, tmp_path, pending(), Verification(VERIFY_FAILED, "转码失败"))
    report = orchestrator.run()

    assert report.failed == 0
    assert report.exit_code == 0


def test_a_submission_acfun_refused_keeps_its_dedup_key(monkeypatch, tmp_path):
    orchestrator, _ = build(monkeypatch, tmp_path, pending(), Verification(VERIFY_REJECTED, "已退回"))
    report = orchestrator.run()

    # Re-uploading the same file would be refused the same way, so this is
    # recorded and surfaced rather than handed back to the retry path.
    assert orchestrator.state.is_published(KEY)
    assert orchestrator.state.published[KEY]["verification"] == VERIFY_REJECTED
    assert len(report.verification_failures) == 1


# --------------------------------------------------------------------------
# When to ask, and when to stop asking
# --------------------------------------------------------------------------


def test_a_fresh_submission_is_not_asked_about_yet(monkeypatch, tmp_path):
    # Two runs in one afternoon must not let the second condemn what the first
    # submitted -- that is exactly the four-minute mistake, with a longer fuse.
    orchestrator, publisher = build(
        monkeypatch, tmp_path, pending(days=0.1), Verification(VERIFY_FAILED, "转码失败")
    )
    orchestrator.run()

    assert publisher.asked == []
    assert orchestrator.state.published[KEY]["verification"] == "pending"


def test_an_answer_that_never_comes_is_eventually_abandoned(monkeypatch, tmp_path):
    orchestrator, publisher = build(
        monkeypatch, tmp_path, pending(days=9), Verification(VERIFY_PENDING, "转码中")
    )
    orchestrator.run()

    # Still published -- it was probably fine -- but out of the queue, which is
    # what stops the pending list growing without bound.
    assert publisher.asked == []
    assert orchestrator.state.is_published(KEY)
    assert orchestrator.state.published[KEY]["verification"] == VERIFY_ABANDONED


def test_an_entry_with_no_remote_id_is_abandoned_rather_than_polled(monkeypatch, tmp_path):
    orchestrator, publisher = build(
        monkeypatch, tmp_path, pending(remote_id=""), Verification(VERIFY_OK, "审核中")
    )
    orchestrator.run()

    assert publisher.asked == []
    assert orchestrator.state.published[KEY]["verification"] == VERIFY_ABANDONED


def test_an_unparseable_timestamp_is_abandoned_rather_than_guessed(monkeypatch, tmp_path):
    orchestrator, _ = build(
        monkeypatch, tmp_path, pending(published_at="not a date"), Verification(VERIFY_OK, "审核中")
    )
    orchestrator.run()

    assert orchestrator.state.published[KEY]["verification"] == VERIFY_ABANDONED


def test_a_dry_run_asks_nothing_and_changes_nothing(monkeypatch, tmp_path):
    orchestrator, publisher = build(
        monkeypatch, tmp_path, pending(), Verification(VERIFY_FAILED, "转码失败"), dry_run=True
    )
    orchestrator.run()

    assert publisher.asked == []
    assert orchestrator.state.published[KEY]["verification"] == "pending"


def test_a_broken_read_back_never_breaks_the_run(monkeypatch, tmp_path):
    orchestrator, _ = build(monkeypatch, tmp_path, pending(), RuntimeError("connection reset"))
    report = orchestrator.run()

    assert orchestrator.state.published[KEY]["verification"] == "pending"
    assert report.exit_code == 0


def test_an_entry_from_a_source_no_longer_configured_is_left_pending(monkeypatch, tmp_path):
    orchestrator, publisher = build(monkeypatch, tmp_path, pending(), Verification(VERIFY_OK, "审核中"))
    orchestrator.state.load()
    orchestrator.state.published["gone-away:xyz"] = pending()
    orchestrator.verify_pending()

    # There is no publisher to ask, so it waits out the give-up window rather
    # than being guessed at either way.
    assert orchestrator.state.published["gone-away:xyz"]["verification"] == "pending"
    assert publisher.asked == ["48764093"]


@pytest.mark.parametrize("outcome", [VERIFY_OK, VERIFY_FAILED, VERIFY_REJECTED])
def test_a_resolved_entry_is_never_asked_about_twice(monkeypatch, tmp_path, outcome):
    orchestrator, publisher = build(monkeypatch, tmp_path, pending(), Verification(outcome, "x"))
    orchestrator.run()
    orchestrator.verify_pending()

    assert publisher.asked == ["48764093"]
