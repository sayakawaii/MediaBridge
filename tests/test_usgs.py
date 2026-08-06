"""USGS earthquake digest tests.

The place strings below are the real shapes the USGS emits: the common
``48 km WSW of X, Country`` form, offshore descriptions with no locality, and
mid-ocean ridge names that have no country at all.
"""

from __future__ import annotations

import pytest

from mediabridge.errors import SourceError
from mediabridge.models import FETCH_NONE
from mediabridge.sources.usgs import UsgsSource, translate_place
from mediabridge.utils.text import strip_html
from tests.test_acfun_html import ALLOWED_TAGS, tags_in


def quake(mag, place, *, depth=35.0, time=1785985931368, tsunami=0, alert=None, kind="earthquake"):
    return {
        "properties": {
            "mag": mag,
            "place": place,
            "time": time,
            "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{abs(hash(place)) % 10**6}",
            "tsunami": tsunami,
            "alert": alert,
            "type": kind,
        },
        "geometry": {"coordinates": [125.0, 5.2, depth]},
    }


FEED = {
    "metadata": {"generated": 1786000000000, "count": 5},
    "features": [
        quake(4.6, "48 km WSW of Sarangani, Philippines"),
        quake(6.3, "south of the Kermadec Islands", depth=226.0, alert="green"),
        quake(7.4, "off the east coast of Honshu, Japan", depth=10.0, tsunami=1),
        quake(5.2, "central Mid-Atlantic Ridge"),
        quake(3.1, "12 km N of Nowhere, Testland"),
    ],
}


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload=None) -> None:
        self.payload = FEED if payload is None else payload
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return _FakeResponse(self.payload)


def build(**options) -> UsgsSource:
    session = options.pop("session", None) or _FakeSession()
    return UsgsSource("usgs-quakes", options, session=session)


# --------------------------------------------------------------------------
# Place-name rendering
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "place, expected",
    [
        ("48 km WSW of Sarangani, Philippines", "菲律宾 Sarangani 西偏南 48 公里"),
        ("15 km N of Honmachi, Japan", "日本 Honmachi 以北 15 公里"),
        ("south of the Kermadec Islands", "克马德克群岛以南海域"),
        ("off the east coast of Honshu, Japan", "日本 本州东岸海域"),
        ("Balleny Islands region", "巴伦尼群岛"),
        ("central Mid-Atlantic Ridge", "中大西洋中脊"),
        ("", "位置不详"),
    ],
)
def test_translate_place(place, expected):
    assert translate_place(place) == expected


def test_unmapped_names_survive_in_english():
    """A wrong guess would be worse than leaving the original spelling alone."""
    assert translate_place("12 km N of Nowhere, Testland") == "Testland Nowhere 以北 12 公里"
    assert translate_place("Atlantis region") == "Atlantis"


# --------------------------------------------------------------------------
# Option validation
# --------------------------------------------------------------------------


def test_level_is_validated():
    with pytest.raises(SourceError, match="level must be one of"):
        build(level="9.9").discover()


def test_period_is_validated():
    with pytest.raises(SourceError, match="period must be one of"):
        build(period="fortnight").discover()


def test_feed_url_follows_the_options():
    source = build(level="significant", period="month")
    source.discover()
    assert source.session.requested == [
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.geojson"
    ]


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def test_min_magnitude_filters_events():
    item = build(min_magnitude=5.0).discover()[0]
    assert item.extra["event_count"] == 3


def test_non_earthquake_events_are_ignored():
    payload = {
        "metadata": {"generated": 1786000000000},
        "features": [quake(5.0, "somewhere", kind="quarry blast")],
    }
    assert build(session=_FakeSession(payload)).discover() == []


def test_empty_feed_yields_nothing():
    payload = {"metadata": {"generated": 1786000000000}, "features": []}
    assert build(session=_FakeSession(payload)).discover() == []


def test_events_are_ordered_by_magnitude():
    body = strip_html(build().discover()[0].body_html)
    assert body.index("M7.4") < body.index("M6.3") < body.index("M5.2")


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_digest_is_a_single_article_item():
    items = build().discover()
    assert len(items) == 1
    item = items[0]
    assert item.fetch_strategy == FETCH_NONE
    assert item.license == "public domain"
    assert tags_in(item.body_html) <= ALLOWED_TAGS


def test_headline_reports_count_and_strongest():
    title = build().discover()[0].title
    assert "5 次" in title
    assert "M7.4" in title


def test_title_template_overrides_the_headline():
    title = build(title_template="地震周报 {date}：{count} 次，最强 M{max_mag}").discover()[0].title
    assert title.startswith("地震周报 ")
    assert "5 次，最强 M7.4" in title


def test_magnitude_buckets_are_summarised():
    body = strip_html(build().discover()[0].body_html)
    assert "M7.0 及以上：1 次" in body
    assert "M6.0 - M6.9：1 次" in body


def test_tsunami_events_are_flagged():
    body = strip_html(build().discover()[0].body_html)
    assert "1 次事件触发了海啸预警评估" in body


def test_alert_level_is_explained():
    body = strip_html(build().discover()[0].body_html)
    assert "绿色（预计影响轻微）" in body


def test_max_entries_caps_the_listing():
    body = build(max_entries=2).discover()[0].body_html
    assert body.count("earthquakes/eventpage/") == 2


def test_source_is_credited():
    body = strip_html(build().discover()[0].body_html)
    assert "美国地质调查局" in body
    assert "公有领域" in body


def test_english_rendering():
    item = build(lang="en").discover()[0]
    body = strip_html(item.body_html)
    assert item.title.startswith("Global earthquake digest")
    assert "the USGS recorded 5 earthquakes" in body
    assert "off the east coast of Honshu, Japan" in body
