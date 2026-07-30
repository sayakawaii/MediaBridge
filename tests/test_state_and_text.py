from __future__ import annotations

import json

import pytest

from mediabridge.config import LimitsConfig
from mediabridge.filters.limits import check, ytdlp_match_filter
from mediabridge.models import MediaItem, PublishResult
from mediabridge.state import State
from mediabridge.utils.text import (
    MAX_TAG_LEN,
    normalise_tags,
    render_template,
    render_within,
    safe_filename,
    strip_html,
    truncate,
)


def _item(**kwargs):
    defaults = {
        "source_name": "src",
        "source_type": "peertube",
        "id": "abc",
        "title": "Title",
        "webpage_url": "https://example.org/w/abc",
    }
    return MediaItem(**{**defaults, **kwargs})


# ---- State ---------------------------------------------------------------


def test_state_round_trip(tmp_path):
    path = tmp_path / "state" / "published.json"
    state = State(path).load()
    assert not state.is_published("src:abc")

    state.record(_item(), PublishResult(ok=True, remote_id="123", url="https://acfun/v/ac123"))
    assert state.save() is True

    reloaded = State(path).load()
    assert reloaded.is_published("src:abc")
    assert reloaded.published["src:abc"]["remote_id"] == "123"


def test_state_is_not_rewritten_when_unchanged(tmp_path):
    path = tmp_path / "published.json"
    assert State(path).load().save() is False
    assert not path.exists()


def test_dedup_key_is_namespaced_by_source():
    assert _item().dedup_key == "src:abc"
    assert _item(source_name="other").dedup_key == "other:abc"


def test_corrupt_state_refuses_to_run(tmp_path):
    path = tmp_path / "published.json"
    path.write_text("{not json", encoding="utf-8")
    # Silently treating this as empty would republish the entire back catalogue.
    with pytest.raises(RuntimeError, match="unreadable"):
        State(path).load()


def test_state_write_is_atomic(tmp_path):
    path = tmp_path / "published.json"
    state = State(path).load()
    state.record(_item(), PublishResult(ok=True, remote_id="1"))
    state.save()
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


# ---- Limits --------------------------------------------------------------


def test_limits_accept_an_item_within_bounds():
    limits = LimitsConfig(max_duration_sec=3600, max_filesize_mb=2000)
    assert check(_item(duration=300, filesize_approx=50 * 1048576), limits) == ""


def test_limits_reject_long_items():
    assert "max_duration_sec" in check(_item(duration=9999), LimitsConfig(max_duration_sec=600))


def test_limits_reject_short_items():
    assert "min_duration_sec" in check(_item(duration=5), LimitsConfig(min_duration_sec=30))


def test_limits_reject_oversized_items():
    limits = LimitsConfig(max_filesize_mb=100)
    assert "max_filesize_mb" in check(_item(filesize_approx=500 * 1048576), limits)


def test_limits_pass_when_metadata_is_absent():
    # An unknown size must not block the item; the fetcher enforces the cap.
    assert check(_item(), LimitsConfig()) == ""


def test_match_filter_expression():
    expression = ytdlp_match_filter(
        LimitsConfig(max_duration_sec=600, min_duration_sec=30, max_filesize_mb=100)
    )
    assert "!is_live" in expression
    assert "duration < 600" in expression
    assert "duration >= 30" in expression
    # '<?' keeps items whose size is unknown rather than discarding them.
    assert "filesize_approx <? 104857600" in expression


# ---- Text ----------------------------------------------------------------


def test_strip_html_preserves_paragraph_breaks():
    assert strip_html("<p>One</p><p>Two</p>") == "One\n\nTwo"
    assert strip_html("a<br/>b") == "a\nb"
    assert strip_html("&amp;&lt;&gt;&nbsp;x") == "&<> x"


def test_truncate_adds_an_ellipsis_only_when_shortening():
    assert truncate("short", 10) == "short"
    assert truncate("abcdefghij", 5) == "abcd…"


def test_render_template_tolerates_missing_keys():
    # A source omitting an optional field must not abort the run.
    assert render_template("{title} by {author}", {"title": "T"}) == "T by "


def test_render_template_survives_stray_braces():
    assert render_template("100% {unclosed", {}) == "100% {unclosed"


def test_render_within_leaves_a_short_result_alone():
    out = render_within("{description}\n来源：{webpage_url}", {"description": "短", "webpage_url": "u"}, 200)
    assert out == "短\n来源：u"


def test_render_within_sacrifices_the_summary_not_the_attribution():
    # An AcFun article has no originalLinkUrl field, so the credit at the end
    # of the description is the only attribution there is. Plain truncation
    # would remove exactly that.
    values = {"description": "长" * 500, "webpage_url": "https://example.org/a"}
    out = render_within("{description}\n\n原始链接：{webpage_url}", values, 200)
    assert len(out) <= 200
    assert out.endswith("原始链接：https://example.org/a")


def test_render_within_falls_back_when_the_boilerplate_alone_overflows():
    values = {"description": "x", "webpage_url": "https://example.org/" + "y" * 300}
    out = render_within("{description} 原始链接：{webpage_url}", values, 200)
    assert len(out) <= 200


def test_normalise_tags_deduplicates_and_strips():
    assert normalise_tags(["#Blender", "blender", " b3d "]) == ["Blender", "b3d"]


def test_normalise_tags_drops_overlong_tags():
    long_tag = "x" * (MAX_TAG_LEN + 1)
    # Clipping would leave a meaningless fragment, so the tag is dropped.
    assert normalise_tags(["ok", long_tag]) == ["ok"]


def test_normalise_tags_respects_the_count_limit():
    assert len(normalise_tags([f"t{i}" for i in range(50)])) == 6


def test_safe_filename_strips_path_separators():
    assert "/" not in safe_filename("a/b:c*d")
    assert safe_filename("") == "media"
