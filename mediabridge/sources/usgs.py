"""USGS earthquake digest (article channel).

Unlike the other article sources this one has no upstream article to repost --
the USGS publishes a machine-readable event feed, and the readable digest is
built here. That makes it the one source whose wording MediaBridge authors
itself, so the output is deliberately plain: counts, magnitudes, locations and
links, with no editorialising about causes or casualties.

Everything the USGS produces as a work of the US federal government is in the
public domain, which is why this source, unlike `feed`, does not demand that
the operator assert a licence.

Place names arrive as English strings shaped like ``48 km WSW of Sarangani,
Philippines``. The Chinese rendering translates the compass bearing and the
trailing country, and leaves the locality in its original spelling rather than
transliterating it badly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape
from typing import Any

from ..errors import SourceError
from ..models import FETCH_NONE, MediaItem
from .base import Source, SourceOptions

log = logging.getLogger(__name__)

FEED_ROOT = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary"
FEED_TIMEOUT_SEC = 60

LEVELS = ("significant", "4.5", "2.5", "1.0", "all")
PERIODS = ("hour", "day", "week", "month")

# fmt: off
BEARINGS_ZH = {
    "N": "以北", "S": "以南", "E": "以东", "W": "以西",
    "NE": "东北", "NW": "西北", "SE": "东南", "SW": "西南",
    "NNE": "北偏东", "ENE": "东偏北", "ESE": "东偏南", "SSE": "南偏东",
    "SSW": "南偏西", "WSW": "西偏南", "WNW": "西偏北", "NNW": "北偏西",
}

#: Only the regions that actually generate M4.5+ events with any regularity.
#: An unmapped name falls through in English, which is better than a wrong guess.
REGIONS_ZH = {
    "Indonesia": "印度尼西亚", "Japan": "日本", "Philippines": "菲律宾",
    "Papua New Guinea": "巴布亚新几内亚", "Chile": "智利", "Peru": "秘鲁",
    "Mexico": "墨西哥", "Tonga": "汤加", "Fiji": "斐济", "Vanuatu": "瓦努阿图",
    "New Zealand": "新西兰", "Russia": "俄罗斯", "China": "中国", "India": "印度",
    "Taiwan": "台湾", "Turkey": "土耳其", "Iran": "伊朗", "Greece": "希腊",
    "Italy": "意大利", "Afghanistan": "阿富汗", "Pakistan": "巴基斯坦",
    "Nepal": "尼泊尔", "Myanmar": "缅甸", "Solomon Islands": "所罗门群岛",
    "Argentina": "阿根廷", "Bolivia": "玻利维亚", "Colombia": "哥伦比亚",
    "Ecuador": "厄瓜多尔", "Guatemala": "危地马拉", "Nicaragua": "尼加拉瓜",
    "Panama": "巴拿马", "Costa Rica": "哥斯达黎加", "El Salvador": "萨尔瓦多",
    "Alaska": "美国阿拉斯加", "California": "美国加利福尼亚", "Hawaii": "美国夏威夷",
    "Puerto Rico": "波多黎各", "Kermadec Islands": "克马德克群岛",
    "South Sandwich Islands": "南桑威奇群岛", "Mariana Islands": "马里亚纳群岛",
    "Kuril Islands": "千岛群岛", "Aleutian Islands": "阿留申群岛",
    "Svalbard and Jan Mayen": "斯瓦尔巴和扬马延", "Iceland": "冰岛",
    "Portugal": "葡萄牙", "Spain": "西班牙", "Algeria": "阿尔及利亚",
    "Morocco": "摩洛哥", "Ethiopia": "埃塞俄比亚", "Tanzania": "坦桑尼亚",
    "Australia": "澳大利亚", "Canada": "加拿大", "Romania": "罗马尼亚",
    "Albania": "阿尔巴尼亚", "Croatia": "克罗地亚", "Georgia": "格鲁吉亚",
    "Azerbaijan": "阿塞拜疆", "Tajikistan": "塔吉克斯坦", "Kyrgyzstan": "吉尔吉斯斯坦",
    "Timor Leste": "东帝汶", "Malaysia": "马来西亚", "Bangladesh": "孟加拉国",
    "Micronesia": "密克罗尼西亚", "Palau": "帕劳", "Samoa": "萨摩亚",
    "New Caledonia": "新喀里多尼亚", "Bonin Islands": "小笠原群岛",
    "Ryukyu Islands": "琉球群岛", "Banda Sea": "班达海", "Molucca Sea": "马鲁古海",
    "Sea of Okhotsk": "鄂霍次克海", "Scotia Sea": "斯科舍海",
    "Balleny Islands": "巴伦尼群岛", "Bouvet Island": "布韦岛",
    "Mid-Indian Ridge": "中印度洋海岭", "Carlsberg Ridge": "卡尔斯伯格海岭",
    "Southwest Indian Ridge": "西南印度洋海岭",
    "Central East Pacific Rise": "东太平洋海隆中部",
    "Southern East Pacific Rise": "东太平洋海隆南部",
    "Pacific-Antarctic Ridge": "太平洋-南极海岭",
    "Reykjanes Ridge": "雷克雅内斯海岭",
    "northern Mid-Atlantic Ridge": "北大西洋中脊",
    "southern Mid-Atlantic Ridge": "南大西洋中脊",
    "central Mid-Atlantic Ridge": "中大西洋中脊",
    "Honshu": "本州", "Hokkaido": "北海道", "Kyushu": "九州", "Shikoku": "四国",
}
# fmt: on

ALERT_ZH = {
    "green": "绿色（预计影响轻微）",
    "yellow": "黄色（可能造成局部损失）",
    "orange": "橙色（可能造成重大损失）",
    "red": "红色（可能造成严重灾害）",
}


#: The USGS also emits offshore descriptions with no locality, e.g.
#: ``south of the Kermadec Islands``.
# fmt: off
OFFSHORE_ZH = {
    "north": "以北海域", "south": "以南海域", "east": "以东海域", "west": "以西海域",
    "northeast": "东北海域", "northwest": "西北海域",
    "southeast": "东南海域", "southwest": "西南海域",
    "off the coast of": "沿岸海域", "off the east coast of": "东岸海域",
    "off the west coast of": "西岸海域",
}
# fmt: on


def _split_offshore(text: str) -> tuple[str, str] | None:
    """Split ``south of the Kermadec Islands`` into its subject and Chinese suffix."""
    lowered = text.lower()
    # Longest marker first, so 'off the east coast of' beats 'east of'.
    for prefix in sorted(OFFSHORE_ZH, key=len, reverse=True):
        marker = f"{prefix} of " if not prefix.endswith(" of") else f"{prefix} "
        if lowered.startswith(marker):
            rest = text[len(marker) :].strip()
            if rest.lower().startswith("the "):
                rest = rest[4:].strip()
            return rest, OFFSHORE_ZH[prefix]
    return None


def _translate_region(region: str) -> str:
    """Translate a bare region string, handling the offshore and 'region' forms."""
    text = region.strip()
    for suffix in (" region", " Region"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]

    offshore = _split_offshore(text)
    if offshore:
        rest, rendered = offshore
        return f"{REGIONS_ZH.get(rest, rest)}{rendered}"

    return REGIONS_ZH.get(text, text)


def translate_place(place: str) -> str:
    """Render a USGS place string in Chinese, leaving unknown names alone."""
    text = (place or "").strip()
    if not text:
        return "位置不详"

    region = text
    locality = ""
    if "," in text:
        locality, _, region = text.rpartition(",")
        locality = locality.strip()

    region_zh = _translate_region(region)

    if not locality:
        return region_zh

    parts = locality.split()
    if len(parts) >= 4 and parts[1] == "km" and parts[3] == "of":
        distance, bearing, spot = parts[0], parts[2], " ".join(parts[4:])
        bearing_zh = BEARINGS_ZH.get(bearing.upper())
        if bearing_zh and spot:
            return f"{region_zh} {spot} {bearing_zh} {distance} 公里"

    offshore = _split_offshore(locality)
    if offshore:
        rest, rendered = offshore
        return f"{region_zh} {REGIONS_ZH.get(rest, rest)}{rendered}"
    return f"{region_zh} {locality}"


class UsgsOptions(SourceOptions):
    level: str = "4.5"
    """USGS feed level: significant, 4.5, 2.5, 1.0 or all."""

    period: str = "week"
    """USGS feed window: hour, day, week or month."""

    min_magnitude: float = 0.0
    """Extra client-side floor, applied on top of the feed level."""

    max_entries: int = 12
    """How many of the strongest events to list individually."""

    lang: str = "zh"
    """``zh`` or ``en``."""

    title_template: str = ""
    """Overrides the built-in headline. ``{count}`` ``{window}`` ``{date}`` ``{max_mag}``."""

    cover_url: str = ""
    limit: int = 1


class UsgsSource(Source):
    type_name = "usgs"
    options_model = UsgsOptions
    description = "USGS earthquake digest (article channel). Public-domain US government data."

    options: UsgsOptions

    def discover(self) -> list[MediaItem]:
        opts = self.options
        if opts.level not in LEVELS:
            raise SourceError(f"[{self.name}] level must be one of {', '.join(LEVELS)}")
        if opts.period not in PERIODS:
            raise SourceError(f"[{self.name}] period must be one of {', '.join(PERIODS)}")

        url = f"{FEED_ROOT}/{opts.level}_{opts.period}.geojson"
        payload = self.get_json(url, timeout=FEED_TIMEOUT_SEC)

        quakes = self._select(payload.get("features") or [])
        if not quakes:
            log.info(
                "[%s] no events at or above M%.1f in the last %s",
                self.name,
                opts.min_magnitude,
                opts.period,
            )
            return []

        generated = payload.get("metadata", {}).get("generated")
        as_of = (
            datetime.fromtimestamp(generated / 1000, tz=timezone.utc)
            if generated
            else datetime.now(timezone.utc)
        )

        title = self._title(quakes, as_of)
        body = self._body(quakes, as_of, url)

        return [
            self.make_item(
                id=f"usgs-{opts.level}-{opts.period}-{as_of:%Y%m%d}",
                title=title,
                webpage_url=f"https://earthquake.usgs.gov/earthquakes/map/?extent={opts.level}",
                description=self._lead(quakes, as_of),
                author="U.S. Geological Survey",
                license="public domain",
                license_url="https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
                published_at=as_of,
                body_html=body,
                fetch_strategy=FETCH_NONE,
                thumbnail_url=opts.cover_url,
                extra={"event_count": len(quakes)},
            )
        ]

    # ---- selection and rendering ----------------------------------------

    def _select(self, features: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for feature in features:
            props = feature.get("properties") or {}
            if props.get("type") != "earthquake":
                continue
            mag = props.get("mag")
            if mag is None or float(mag) < self.options.min_magnitude:
                continue
            geometry = (feature.get("geometry") or {}).get("coordinates") or [None, None, None]
            out.append(
                {
                    "mag": float(mag),
                    "place": props.get("place") or "",
                    "time": props.get("time"),
                    "url": props.get("url") or "",
                    "depth": geometry[2] if len(geometry) > 2 else None,
                    "tsunami": bool(props.get("tsunami")),
                    "alert": props.get("alert"),
                }
            )
        out.sort(key=lambda q: q["mag"], reverse=True)
        return out

    @property
    def _window_zh(self) -> str:
        return {"hour": "一小时", "day": "24 小时", "week": "一周", "month": "一个月"}[self.options.period]

    @property
    def _window_en(self) -> str:
        return {"hour": "hour", "day": "day", "week": "week", "month": "month"}[self.options.period]

    def _title(self, quakes: list[dict[str, Any]], as_of: datetime) -> str:
        strongest = quakes[0]["mag"]
        if self.options.title_template:
            return self.options.title_template.format(
                count=len(quakes),
                window=self._window_zh if self.options.lang == "zh" else self._window_en,
                date=f"{as_of:%Y-%m-%d}",
                max_mag=f"{strongest:.1f}",
            )
        if self.options.lang == "zh":
            return f"全球地震观测：过去{self._window_zh}记录到 {len(quakes)} 次，最强 M{strongest:.1f}"
        return (
            f"Global earthquake digest: {len(quakes)} events in the past "
            f"{self._window_en}, strongest M{strongest:.1f}"
        )

    def _lead(self, quakes: list[dict[str, Any]], as_of: datetime) -> str:
        strongest = quakes[0]
        if self.options.lang == "zh":
            return (
                f"美国地质调查局的观测数据显示，截至 {as_of:%Y年%m月%d日}，"
                f"过去{self._window_zh}全球共记录到 {len(quakes)} 次地震，"
                f"其中最强的一次为 M{strongest['mag']:.1f}，发生在{translate_place(strongest['place'])}。"
            )
        return (
            f"As of {as_of:%d %B %Y}, the USGS recorded {len(quakes)} earthquakes worldwide over "
            f"the past {self._window_en}. The strongest was M{strongest['mag']:.1f} at "
            f"{strongest['place']}."
        )

    def _body(self, quakes: list[dict[str, Any]], as_of: datetime, feed_url: str) -> str:
        zh = self.options.lang == "zh"
        parts = [f"<p>{escape(self._lead(quakes, as_of))}</p>"]

        buckets = [(7.0, "M7.0 及以上", "M7.0 and above"), (6.0, "M6.0 - M6.9", "M6.0 - M6.9")]
        for floor, label_zh, label_en in buckets:
            ceiling = floor + 1.0 if floor < 7.0 else 99.0
            count = sum(1 for q in quakes if floor <= q["mag"] < ceiling)
            if count:
                label = label_zh if zh else label_en
                line = f"{label}：{count} 次" if zh else f"{label}: {count}"
                parts.append(f"<p>{escape(line)}</p>")

        tsunami = [q for q in quakes if q["tsunami"]]
        if tsunami:
            note = (
                f"其中 {len(tsunami)} 次事件触发了海啸预警评估。"
                if zh
                else f"{len(tsunami)} of these events triggered tsunami evaluation."
            )
            parts.append(f"<p>{escape(note)}</p>")

        parts.append(f"<h3>{'显著地震' if zh else 'Notable events'}</h3>")
        for quake in quakes[: self.options.max_entries]:
            parts.append(f"<p>{escape(self._describe(quake))}</p>")

        footer = (
            f"数据来源：美国地质调查局（USGS）地震目录，属公有领域。原始数据：{feed_url}"
            if zh
            else f"Source: USGS earthquake catalog, public domain. Raw feed: {feed_url}"
        )
        parts.append("<hr>")
        parts.append(f"<p>{escape(footer)}</p>")
        return "".join(parts)

    def _describe(self, quake: dict[str, Any]) -> str:
        zh = self.options.lang == "zh"
        when = datetime.fromtimestamp(quake["time"] / 1000, tz=timezone.utc) if quake.get("time") else None
        depth = quake.get("depth")

        if zh:
            bits = [f"M{quake['mag']:.1f}", translate_place(quake["place"])]
            if depth is not None:
                bits.append(f"震源深度 {depth:.0f} 公里")
            if when:
                bits.append(f"{when:%m月%d日 %H:%M} UTC")
            if quake.get("alert") in ALERT_ZH:
                bits.append(f"影响评估：{ALERT_ZH[quake['alert']]}")
            if quake["tsunami"]:
                bits.append("已触发海啸评估")
        else:
            bits = [f"M{quake['mag']:.1f}", quake["place"] or "location unknown"]
            if depth is not None:
                bits.append(f"depth {depth:.0f} km")
            if when:
                bits.append(f"{when:%d %b %H:%M} UTC")
            if quake.get("alert"):
                bits.append(f"alert: {quake['alert']}")
            if quake["tsunami"]:
                bits.append("tsunami evaluation triggered")

        line = " · ".join(bits)
        return f"{line} · {quake['url']}" if quake.get("url") else line
