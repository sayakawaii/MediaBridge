"""Run loop: discover, filter, fetch, publish, record."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, SourceConfig
from .errors import FetchError, MediaBridgeError, PublishError, SkipItem, SourceError
from .fetchers.base import fetch
from .filters import limits as limit_filter
from .models import FetchedMedia, MediaItem
from .publishers.base import PublishContext, build_publisher
from .sources.registry import build_source
from .state import State
from .utils.http import build_session

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    discovered: int = 0
    skipped_duplicate: int = 0
    skipped_filtered: int = 0
    published: int = 0
    failed: int = 0
    published_urls: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        # A run that publishes nothing is normal (everything already seen);
        # only genuine failures should fail the workflow.
        return 1 if self.failed else 0

    def summary(self) -> str:
        return (
            f"discovered={self.discovered} published={self.published} "
            f"duplicate={self.skipped_duplicate} filtered={self.skipped_filtered} failed={self.failed}"
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

        budget = max_items if max_items is not None else self.config.limits.max_items_per_run
        work_root = Path(self.config.work_dir)

        for source_config in self.config.enabled_sources():
            if only and source_config.name not in only:
                continue
            if budget is not None and self.report.published >= budget:
                log.info("Reached max_items_per_run=%s; stopping.", budget)
                break

            remaining = None if budget is None else budget - self.report.published
            self._run_source(source_config, work_root, remaining)

        if self.state.save():
            log.info("State written to %s", self.config.state_file)

        log.info("Run complete: %s", self.report.summary())
        for url in self.report.published_urls:
            log.info("  published -> %s", url)
        return self.report

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

        for item in candidates:
            if remaining is not None and published_here >= remaining:
                log.info("[%s] reached the run-wide item budget", source_config.name)
                return
            if per_source and published_here >= per_source:
                log.info("[%s] reached max_items_per_source=%d", source_config.name, per_source)
                return

            if self._process(item, source_config, work_root):
                published_here += 1

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
