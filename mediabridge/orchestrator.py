"""Run loop: discover, filter, fetch, publish, record."""

from __future__ import annotations

import logging
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config, SourceConfig
from .errors import FetchError, MediaBridgeError, PublishError, SkipItem, SourceError
from .fetchers.base import fetch
from .filters import limits as limit_filter
from .models import FetchedMedia, MediaItem
from .publishers.base import (
    VERIFY_FAILED,
    VERIFY_OK,
    VERIFY_PENDING,
    VERIFY_REJECTED,
    PublishContext,
    build_publisher,
)
from .sources.registry import build_source
from .state import State
from .utils.http import build_session

log = logging.getLogger(__name__)

#: Failures do not count against `max_items_per_source`, so a source that is
#: systematically broken -- every description over AcFun's limit, a dead CDN --
#: keeps being handed the next candidate. One such source once attempted six
#: videos under `max_items_per_source: 1` and spent fifty minutes uploading
#: files that were all rejected at submission. Three in a row is enough to call
#: it: the fourth attempt has never been the one that worked.
MAX_CONSECUTIVE_FAILURES = 3

#: Leave a submission alone for this long before asking what became of it.
#: Runs are a day apart so this normally costs nothing, but two runs in one
#: afternoon must not let the second one condemn what the first submitted --
#: that is the mistake deferring the check exists to avoid.
VERIFY_MIN_AGE = timedelta(hours=6)

#: Stop asking after this long. Something that has neither succeeded nor failed
#: in a week is not going to resolve itself, and the pending list has to be
#: bounded or a platform that stops answering grows it forever.
VERIFY_GIVE_UP_AFTER = timedelta(days=7)

#: Written into the ledger for an entry that ran out of that patience. Not a
#: failure -- the submission may well be fine -- just no longer worth asking.
VERIFY_ABANDONED = "abandoned"


@dataclass
class RunReport:
    discovered: int = 0
    skipped_duplicate: int = 0
    skipped_filtered: int = 0
    published: int = 0
    failed: int = 0
    verified: int = 0
    published_urls: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    verification_failures: list[str] = field(default_factory=list)
    """Submissions an *earlier* run made that the platform then threw out.

    Kept apart from `failures` deliberately. They do not fail this run: the
    item's dedup key has already been released, so the retry is automatic and
    the only outstanding action is a human deleting the dead submission, which
    the log names. Failing the job for that would make every run after a bad
    one red for something already handled.
    """

    @property
    def exit_code(self) -> int:
        # A run that publishes nothing is normal (everything already seen);
        # only genuine failures should fail the workflow.
        return 1 if self.failed else 0

    def summary(self) -> str:
        return (
            f"discovered={self.discovered} published={self.published} "
            f"duplicate={self.skipped_duplicate} filtered={self.skipped_filtered} "
            f"failed={self.failed} verified={self.verified}"
        )


class Orchestrator:
    def __init__(self, config: Config, dry_run: bool = False, fetch_on_dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run

        #: A dry run answers "what would be published"; downloading gigabytes
        #: to answer it is pure waste, so the fetch stage is skipped unless the
        #: caller explicitly wants to rehearse it.
        self.fetch_on_dry_run = fetch_on_dry_run
        self.state = State(config.state_file)
        self.ctx = PublishContext(config=config, dry_run=dry_run)
        self.session = build_session()
        self.report = RunReport()

    def run(self, only: list[str] | None = None, max_items: int | None = None) -> RunReport:
        self.state.load()
        self.verify_pending()

        budget = max_items if max_items is not None else self.config.limits.max_items_per_run
        work_root = Path(self.config.work_dir)

        spent_per_target: Counter[str] = Counter()

        for source_config in self.config.enabled_sources():
            if only and source_config.name not in only:
                continue
            if budget is not None and self.report.published >= budget:
                log.info("Reached max_items_per_run=%s; stopping.", budget)
                break

            target = source_config.publish.target
            target_cap = self.config.limits.target_budget(target)
            if target_cap is not None and spent_per_target[target] >= target_cap:
                # Skipping here rather than inside _run_source matters: discovery
                # costs upstream HTTP requests that would be thrown away.
                log.info(
                    "--- source '%s' skipped: target '%s' has spent its budget of %d ---",
                    source_config.name,
                    target,
                    target_cap,
                )
                continue

            remaining = self._remaining_for(target, budget, spent_per_target)
            published_before = self.report.published
            self._run_source(source_config, work_root, remaining)
            spent_per_target[target] += self.report.published - published_before

        if self.state.save():
            log.info("State written to %s", self.config.state_file)

        log.info("Run complete: %s", self.report.summary())
        for url in self.report.published_urls:
            log.info("  published -> %s", url)
        return self.report

    # ---- Deferred verification ------------------------------------------

    def verify_pending(self) -> None:
        """Settle submissions earlier runs made but could not judge.

        AcFun keeps transcoding long after ``createDouga`` returns, and how long
        depends on the file: a three-minute clip is done in seconds, half an
        hour of 1080p60 is not. A run used to wait four minutes and read "still
        queued" as "failed", which threw away three perfectly good submissions
        in a single morning. By the next run the queue has had a day to drain,
        so the same question has an answer worth acting on and costs one HTTP
        request per outstanding item instead of four minutes each.
        """
        pending = self.state.pending_verification()
        if not pending:
            return
        if self.dry_run:
            log.info("[dry-run] would check %d submission(s) awaiting verification", len(pending))
            return

        log.info("--- checking %d submission(s) awaiting verification ---", len(pending))
        targets = {source.name: source.publish.target for source in self.config.sources}
        now = datetime.now(timezone.utc)

        for key, entry in pending:
            try:
                self._verify_one(key, entry, targets, now)
            except Exception as exc:  # noqa: BLE001 - a check-up must never break the run
                log.warning("Could not verify %s: %s; will ask again next run", key, exc)

    def _verify_one(self, key: str, entry: dict, targets: dict[str, str], now: datetime) -> None:
        remote_id = str(entry.get("remote_id") or "")
        label = f"{key} -> ac{remote_id}" if remote_id else key
        age = _age_of(entry.get("published_at"), now)

        if not remote_id or age is None or age > VERIFY_GIVE_UP_AFTER:
            # Without an id or a plausible age there is nothing to ask about,
            # and after a week the answer has stopped being interesting. Either
            # way it stays published; it just leaves the queue.
            log.warning("Giving up on verifying %s; leaving it recorded as published", label)
            self.state.resolve_verification(key, VERIFY_ABANDONED)
            return

        if age < VERIFY_MIN_AGE:
            log.debug("%s is only %s old; too early to judge", label, _hours(age))
            return

        target = targets.get(key.split(":", 1)[0])
        if target is None:
            log.debug("%s belongs to a source no longer configured; cannot verify it", label)
            return

        publisher = build_publisher(target, self.ctx)
        outcome = publisher.verify(remote_id)

        if outcome.state == VERIFY_PENDING:
            log.info("%s is still unresolved after %s (%s)", label, _hours(age), outcome.detail)
            return

        self.state.resolve_verification(key, outcome.state)
        if outcome.state == VERIFY_OK:
            self.report.verified += 1
            log.info("%s verified: %s", label, outcome.detail)
            return

        url = entry.get("remote_url") or f"https://www.acfun.cn/v/ac{remote_id}"
        if outcome.state == VERIFY_REJECTED:
            # Nothing to retry -- the same file would be refused again -- so the
            # entry stays and a human decides what to do with the submission.
            log.error("%s was refused by the platform (%s): %s", label, outcome.detail, url)
            self.report.verification_failures.append(f"ac{remote_id} refused ({outcome.detail}): {url}")
            return

        if outcome.state == VERIFY_FAILED:
            self.state.release(key)
            log.error(
                "%s still reports '%s' %s after it was submitted, so the video will never play. "
                "Delete ac%s by hand: %s. Its dedup key has been released, so a later run will "
                "try the item again.",
                label,
                outcome.detail,
                _hours(age),
                remote_id,
                url,
            )
            self.report.verification_failures.append(
                f"ac{remote_id} failed to transcode ({outcome.detail}); delete it by hand: {url}"
            )

    def _remaining_for(self, target: str, budget: int | None, spent_per_target: Counter[str]) -> int | None:
        """How many items this target may still publish, under both caps."""
        caps = []
        if budget is not None:
            caps.append(budget - self.report.published)
        target_cap = self.config.limits.target_budget(target)
        if target_cap is not None:
            caps.append(target_cap - spent_per_target[target])
        return min(caps) if caps else None

    def refresh(self, only: list[str] | None = None, max_items: int | None = None) -> RunReport:
        """Re-render already-published items and re-submit them in place.

        For when the rendering changed rather than the source did -- a template
        edit, or a platform quirk discovered after the fact. Items *not* yet in
        the ledger are left alone; this never publishes anything new.
        """
        self.state.load()
        work_root = Path(self.config.work_dir)

        for source_config in self.config.enabled_sources():
            if only and source_config.name not in only:
                continue
            if max_items is not None and self.report.published >= max_items:
                break
            self._refresh_source(source_config, work_root, max_items)

        if self.state.save():
            log.info("State written to %s", self.config.state_file)
        log.info("Refresh complete: %s", self.report.summary())
        return self.report

    def _refresh_source(self, source_config: SourceConfig, work_root: Path, max_items: int | None) -> None:
        log.info("--- refreshing source '%s' (%s) ---", source_config.name, source_config.type)
        try:
            source = build_source(
                source_config.name, source_config.type, source_config.options, session=self.session
            )
            source.limits = self.config.limits
            candidates = source.discover()
        except (SourceError, MediaBridgeError) as exc:
            log.error("Source '%s' failed during discovery: %s", source_config.name, exc)
            self.report.failed += 1
            self.report.failures.append(f"{source_config.name}: {exc}")
            return

        self.report.discovered += len(candidates)

        for item in candidates:
            if max_items is not None and self.report.published >= max_items:
                return
            remote_id = self.state.remote_id(item.dedup_key)
            if not remote_id:
                log.debug("[%s] not published yet, skipping: %s", source_config.name, item.title)
                self.report.skipped_filtered += 1
                continue
            self._refresh_item(item, source_config, work_root, remote_id)

    def _refresh_item(
        self, item: MediaItem, source_config: SourceConfig, work_root: Path, remote_id: str
    ) -> None:
        log.info("[%s] refreshing ac%s: %s", source_config.name, remote_id, item.title)
        work_dir = work_root / source_config.name / _slug(item.id)

        try:
            publisher = build_publisher(source_config.publish.target, self.ctx)
            publisher.validate_config(source_config.publish)

            if self.dry_run and not self.fetch_on_dry_run:
                fetched = FetchedMedia(item=item)
            else:
                fetched = fetch(item, work_dir, self.config.limits)

            result = publisher.republish(fetched, source_config.publish, remote_id)
        except SkipItem as exc:
            log.info("[%s] skipping %r: %s", source_config.name, item.title, exc)
            self.report.skipped_filtered += 1
            return
        except (FetchError, PublishError, MediaBridgeError) as exc:
            log.error("[%s] failed to refresh %r: %s", source_config.name, item.title, exc)
            self.report.failed += 1
            self.report.failures.append(f"{item.webpage_url}: {exc}")
            return
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        self.report.published += 1
        self.report.published_urls.append(result.url or item.webpage_url)
        if not result.dry_run:
            self.state.mark_refreshed(item.dedup_key)

    def _run_source(self, source_config: SourceConfig, work_root: Path, remaining: int | None) -> None:
        log.info("--- source '%s' (%s) ---", source_config.name, source_config.type)

        try:
            source = build_source(
                source_config.name, source_config.type, source_config.options, session=self.session
            )
            source.limits = self.config.limits
            candidates = source.discover()
        except (SourceError, MediaBridgeError) as exc:
            # One broken source must not take down the whole scheduled run.
            log.error("Source '%s' failed during discovery: %s", source_config.name, exc)
            self.report.failed += 1
            self.report.failures.append(f"{source_config.name}: {exc}")
            return

        self.report.discovered += len(candidates)

        per_source = self.config.limits.max_items_per_source
        published_here = 0
        consecutive_failures = 0

        for item in candidates:
            if remaining is not None and published_here >= remaining:
                log.info("[%s] used up the item budget available to it", source_config.name)
                return
            if per_source and published_here >= per_source:
                log.info("[%s] reached max_items_per_source=%d", source_config.name, per_source)
                return

            failed_before = self.report.failed
            if self._process(item, source_config, work_root):
                published_here += 1
                consecutive_failures = 0
            elif self.report.failed > failed_before:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error(
                        "[%s] %d item(s) in a row failed; abandoning this source for the rest of "
                        "the run rather than paying for more uploads that will not land.",
                        source_config.name,
                        consecutive_failures,
                    )
                    return

    def _process(self, item: MediaItem, source_config: SourceConfig, work_root: Path) -> bool:
        if self.state.is_published(item.dedup_key):
            log.debug("[%s] already published: %s", source_config.name, item.title)
            self.report.skipped_duplicate += 1
            return False

        reason = limit_filter.check(item, self.config.limits)
        if reason:
            log.info("[%s] skipping %r: %s", source_config.name, item.title, reason)
            self.report.skipped_filtered += 1
            return False

        log.info("[%s] processing: %s", source_config.name, item.summary())
        work_dir = work_root / source_config.name / _slug(item.id)

        try:
            publisher = build_publisher(source_config.publish.target, self.ctx)
            publisher.validate_config(source_config.publish)

            if self.dry_run and not self.fetch_on_dry_run:
                fetched = FetchedMedia(item=item)
            else:
                fetched = fetch(item, work_dir, self.config.limits)

            result = publisher.publish(fetched, source_config.publish)
        except SkipItem as exc:
            log.info("[%s] skipping %r: %s", source_config.name, item.title, exc)
            self.report.skipped_filtered += 1
            return False
        except (FetchError, PublishError, MediaBridgeError) as exc:
            log.error("[%s] failed on %r: %s", source_config.name, item.title, exc)
            if getattr(exc, "hint", ""):
                log.error("  hint: %s", exc.hint)
            self.report.failed += 1
            self.report.failures.append(f"{item.webpage_url}: {exc}")
            return False
        finally:
            # Reclaim disk before the next item; runners guarantee only 14 GB.
            shutil.rmtree(work_dir, ignore_errors=True)

        if not result.ok:
            self.report.failed += 1
            self.report.failures.append(f"{item.webpage_url}: {result.message}")
            return False

        self.report.published += 1
        self.report.published_urls.append(result.url or item.webpage_url)

        if not result.dry_run:
            self.state.record(item, result)
        return True


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value)[:64] or "item"


def _age_of(stamp: object, now: datetime) -> timedelta | None:
    """How long ago `stamp` was, or None when it cannot be read as a time."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return now - when


def _hours(age: timedelta) -> str:
    return f"{age.total_seconds() / 3600:.0f}h"
