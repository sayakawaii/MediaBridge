"""Per-target publishing budget tests.

These exist because of a live regression: `config.yaml` listed six video
sources ahead of three article sources under a single `max_items_per_run: 4`,
and since sources are visited in file order with no rotation, every slot went
to video and not one article was published for days. The first test below is
that exact arrangement.
"""

from __future__ import annotations

import pytest

from mediabridge.config import Config
from mediabridge.models import MediaItem, PublishResult
from mediabridge.orchestrator import Orchestrator

VIDEO = "acfun_video"
ARTICLE = "acfun_article"


class _FakeSource:
    """Yields `count` candidates, none of which are ever duplicates."""

    def __init__(self, name: str, count: int) -> None:
        self.name = name
        self.count = count
        self.limits = None

    def discover(self) -> list[MediaItem]:
        return [
            MediaItem(
                source_name=self.name,
                source_type="fake",
                id=f"{self.name}-{i}",
                title=f"{self.name} item {i}",
                webpage_url=f"https://example.org/{self.name}/{i}",
            )
            for i in range(self.count)
        ]


class _FakePublisher:
    def __init__(self, target: str) -> None:
        self.target = target

    def validate_config(self, publish_config) -> None:
        pass

    def publish(self, fetched, publish_config) -> PublishResult:
        return PublishResult(
            ok=True,
            remote_id="1",
            url=f"https://acfun.example/{fetched.item.id}",
            dry_run=True,
        )


@pytest.fixture
def wiring(monkeypatch, tmp_path):
    """Replace source and publisher construction, and record what discovers."""
    discovered: list[str] = []

    def fake_build_source(name, type_name, options, session=None):
        discovered.append(name)
        return _FakeSource(name, options.get("count", 5))

    monkeypatch.setattr("mediabridge.orchestrator.build_source", fake_build_source)
    monkeypatch.setattr(
        "mediabridge.orchestrator.build_publisher", lambda target, ctx: _FakePublisher(target)
    )
    return discovered


def build_config(tmp_path, sources: list[tuple[str, str]], **limits) -> Config:
    """`sources` is a list of (name, publish target) in the order they appear."""
    limits.setdefault("max_items_per_source", 1)
    return Config.model_validate(
        {
            "version": 1,
            "state_file": str(tmp_path / "state.json"),
            "work_dir": str(tmp_path / "work"),
            "limits": limits,
            "sources": [
                {
                    "name": name,
                    "type": "fake",
                    "options": {"count": 5},
                    "publish": {"target": target, "channel_id": 1, "realm_id": 1},
                }
                for name, target in sources
            ],
        }
    )


def published_names(report) -> list[str]:
    return [url.rsplit("/", 1)[-1] for url in report.published_urls]


def counts_by_target(config, report) -> dict[str, int]:
    """Tally publishes per publish target, via the source each item came from."""
    targets = {source.name: source.publish.target for source in config.sources}
    counts: dict[str, int] = {}
    for name in published_names(report):
        target = targets[name.rsplit("-", 1)[0]]
        counts[target] = counts.get(target, 0) + 1
    return counts


def run(config, **kwargs):
    return Orchestrator(config, dry_run=True).run(**kwargs)


# --------------------------------------------------------------------------
# The regression
# --------------------------------------------------------------------------


def test_articles_are_reached_even_when_video_sources_come_first(wiring, tmp_path):
    """The live bug: six video sources ahead of three article ones.

    The numbers mirror `config.yaml`, where the article cap is one slot per
    article source so all three publish daily.
    """
    config = build_config(
        tmp_path,
        [
            ("blender", VIDEO),
            ("tilvids", VIDEO),
            ("makertube", VIDEO),
            ("nasa-video", VIDEO),
            ("archive", VIDEO),
            ("commons", VIDEO),
            ("un-news", ARTICLE),
            ("usgs", ARTICLE),
            ("nasa-news", ARTICLE),
        ],
        max_items_per_run=7,
        max_items_per_target={VIDEO: 4, ARTICLE: 3},
    )
    report = run(config)

    assert report.published == 7
    assert counts_by_target(config, report) == {VIDEO: 4, ARTICLE: 3}
    assert published_names(report)[-3:] == ["un-news-0", "usgs-0", "nasa-news-0"]


def test_a_single_global_budget_still_starves_the_later_target(wiring, tmp_path):
    """Documents the behaviour the per-target caps exist to correct."""
    config = build_config(
        tmp_path,
        [("v1", VIDEO), ("v2", VIDEO), ("v3", VIDEO), ("v4", VIDEO), ("article", ARTICLE)],
        max_items_per_run=4,
    )
    report = run(config)

    assert report.published == 4
    assert counts_by_target(config, report) == {VIDEO: 4}


# --------------------------------------------------------------------------
# Budget arithmetic
# --------------------------------------------------------------------------


def test_targets_are_budgeted_independently(wiring, tmp_path):
    config = build_config(
        tmp_path,
        [("v1", VIDEO), ("v2", VIDEO), ("v3", VIDEO), ("a1", ARTICLE), ("a2", ARTICLE)],
        max_items_per_run=99,
        max_items_per_target={VIDEO: 2, ARTICLE: 1},
    )
    report = run(config)

    assert counts_by_target(config, report) == {VIDEO: 2, ARTICLE: 1}


def test_a_spent_target_is_skipped_before_discovery(wiring, tmp_path):
    """Discovery costs upstream requests, so an exhausted target must not run it."""
    config = build_config(
        tmp_path,
        [("v1", VIDEO), ("v2", VIDEO), ("v3", VIDEO), ("a1", ARTICLE)],
        max_items_per_run=99,
        max_items_per_target={VIDEO: 1, ARTICLE: 1},
    )
    run(config)

    # v2 and v3 are past the video cap and must never have been constructed.
    assert wiring == ["v1", "a1"]


def test_run_wide_ceiling_still_applies_over_per_target_caps(wiring, tmp_path):
    config = build_config(
        tmp_path,
        [("v1", VIDEO), ("v2", VIDEO), ("a1", ARTICLE), ("a2", ARTICLE)],
        max_items_per_run=3,
        max_items_per_target={VIDEO: 4, ARTICLE: 4},
    )
    report = run(config)
    assert report.published == 3


def test_max_items_per_source_still_caps_each_source(wiring, tmp_path):
    config = build_config(
        tmp_path,
        [("v1", VIDEO), ("a1", ARTICLE)],
        max_items_per_run=99,
        max_items_per_source=2,
        max_items_per_target={VIDEO: 99, ARTICLE: 99},
    )
    report = run(config)
    assert report.published == 4  # two sources, two items each


def test_cli_max_items_override_still_wins(wiring, tmp_path):
    config = build_config(
        tmp_path,
        [("v1", VIDEO), ("v2", VIDEO), ("a1", ARTICLE)],
        max_items_per_run=99,
        max_items_per_target={VIDEO: 4, ARTICLE: 4},
    )
    report = run(config, max_items=2)
    assert report.published == 2


def test_an_unbudgeted_target_is_limited_only_by_the_run_ceiling(wiring, tmp_path):
    """A target absent from the mapping must not be treated as having zero slots."""
    config = build_config(
        tmp_path,
        [("a1", ARTICLE), ("a2", ARTICLE), ("a3", ARTICLE)],
        max_items_per_run=99,
        max_items_per_target={VIDEO: 1},
    )
    report = run(config)
    assert report.published == 3


# --------------------------------------------------------------------------
# Backwards compatibility
# --------------------------------------------------------------------------


def test_absent_mapping_keeps_the_old_single_budget_behaviour(wiring, tmp_path):
    config = build_config(tmp_path, [("v1", VIDEO), ("v2", VIDEO), ("v3", VIDEO)], max_items_per_run=2)
    report = run(config)
    assert report.published == 2
    assert config.limits.max_items_per_target == {}


def test_target_budget_returns_none_when_unset():
    limits = Config().limits
    assert limits.target_budget(VIDEO) is None
    assert limits.max_items_per_target == {}


def test_only_filter_still_restricts_to_named_sources(wiring, tmp_path):
    config = build_config(
        tmp_path,
        [("v1", VIDEO), ("a1", ARTICLE)],
        max_items_per_run=99,
        max_items_per_target={VIDEO: 4, ARTICLE: 4},
    )
    report = run(config, only=["a1"])
    assert published_names(report) == ["a1-0"]
