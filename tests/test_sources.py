"""Source adapter tests.

These run against recorded response shapes rather than the network. Archive.org
and Wikimedia are unreachable from some networks, and no CI job should depend
on a third party's uptime to go green.
"""

from __future__ import annotations

import pytest

from mediabridge.config import LimitsConfig
from mediabridge.models import FETCH_DIRECT, FETCH_YTDLP
from mediabridge.sources.archive_org import ArchiveOrgSource
from mediabridge.sources.nasa import NasaSource
from mediabridge.sources.peertube import PeerTubeSource
from mediabridge.sources.wikimedia import WikimediaSource


def _stub(source_class, responses):
    """Build a source whose HTTP calls are answered from `responses`."""

    class Stubbed(source_class):
        def get_json(self, url, params=None, timeout=30):
            for fragment, payload in responses.items():
                if fragment in url:
                    return payload
            raise AssertionError(f"unexpected request to {url}")

    return Stubbed


# --------------------------------------------------------------------------
# PeerTube

PEERTUBE_LIST = {
    "data": [
        {
            "uuid": "u-by",
            "shortUUID": "s-by",
            "name": "CC BY clip",
            "licence": {"id": 1, "label": "CC BY"},
        },
        {
            "uuid": "u-nc",
            "shortUUID": "s-nc",
            "name": "NC clip",
            "licence": {"id": 4, "label": "CC BY-NC"},
        },
        {
            "uuid": "u-arr",
            "shortUUID": "s-arr",
            "name": "Closed",
            "licence": {"id": 9, "label": "All rights"},
        },
    ]
}
PEERTUBE_DETAIL = {
    "uuid": "u-by",
    "name": "CC BY clip",
    "description": "<p>Made with <b>Blender</b></p>",
    "duration": 237,
    "licence": {"id": 1},
    "account": {"displayName": "Blender"},
    "tags": ["b3d"],
    "previewPath": "/lazy-static/previews/x.jpg",
    "files": [{"size": 1000}, {"size": 22_000_000}],
    "publishedAt": "2019-04-16T10:00:00.000Z",
}


def test_peertube_filters_by_licence_enum():
    source = _stub(PeerTubeSource, {"/videos/u-by": PEERTUBE_DETAIL, "/videos": PEERTUBE_LIST})(
        "blender", {"host": "video.blender.org", "licence_allow": [1, 2, 3, 7, 8]}
    )
    items = source.discover()
    # NonCommercial and All-rights-reserved are re-checked client-side in case
    # the instance ignores licenceOneOf.
    assert [i.id for i in items] == ["u-by"]


def test_peertube_builds_a_complete_item():
    source = _stub(PeerTubeSource, {"/videos/u-by": PEERTUBE_DETAIL, "/videos": PEERTUBE_LIST})(
        "blender", {"host": "video.blender.org"}
    )
    item = source.discover()[0]

    assert item.title == "CC BY clip"
    assert item.license == "CC BY 4.0"
    assert item.duration == 237
    assert item.author == "Blender"
    assert item.description == "Made with Blender"
    assert item.webpage_url == "https://video.blender.org/w/s-by"
    assert item.thumbnail_url.startswith("https://video.blender.org/")
    # Largest rendition, so the size check is a worst case rather than a guess.
    assert item.filesize_approx == 22_000_000
    assert item.fetch_strategy == FETCH_YTDLP


def test_peertube_uses_the_instance_agnostic_pseudo_url():
    source = _stub(PeerTubeSource, {"/videos/u-by": PEERTUBE_DETAIL, "/videos": PEERTUBE_LIST})(
        "blender", {"host": "video.blender.org"}
    )
    # The pseudo-URL form works on instances absent from yt-dlp's host list.
    assert source.discover()[0].download_url == "peertube:video.blender.org:u-by"


def test_peertube_requests_the_bracketed_licence_array():
    captured = {}

    class Recording(PeerTubeSource):
        def get_json(self, url, params=None, timeout=30):
            if "/videos/" in url:
                return PEERTUBE_DETAIL
            captured.update(params or {})
            return PEERTUBE_LIST

    Recording("blender", {"host": "video.blender.org", "licence_allow": [1, 7]}).discover()
    # Current PeerTube rejects the comma-joined form outright.
    assert captured["licenceOneOf[]"] == ["1", "7"]
    assert "licenceOneOf" not in captured


# --------------------------------------------------------------------------
# Internet Archive

IA_SEARCH = {
    "response": {
        "docs": [
            {
                "identifier": "Cheerios1960",
                "title": "Cheerios (1960)",
                "licenseurl": "http://creativecommons.org/licenses/publicdomain/",
                "publicdate": "2004-03-08T00:00:00Z",
                "creator": "Filmways",
                "description": "A <b>cereal</b> ad.",
            },
            {
                "identifier": "NoLicence",
                "title": "No licence field",
                "publicdate": "2020-01-01T00:00:00Z",
            },
            {
                "identifier": "NcOnly",
                "title": "NC only",
                "licenseurl": "https://creativecommons.org/licenses/by-nc/4.0/",
                "publicdate": "2021-01-01T00:00:00Z",
            },
        ]
    }
}
IA_METADATA = {
    "metadata": {"subject": "ads;1960s"},
    "files": [
        {"name": "thumb.png", "size": "12000"},
        {"name": "c_512kb.mp4", "size": "4400000", "format": "512Kb MPEG4", "length": "1:00"},
        {"name": "c.mp4", "size": "6300000", "format": "h.264", "length": "60.5"},
        {"name": "c_hires.mp4", "size": "400000000", "format": "HiRes MPEG4", "length": "1:00"},
        {"name": "c.ogv", "size": "5000000"},
    ],
}


def _archive(**options):
    source = _stub(ArchiveOrgSource, {"/metadata/": IA_METADATA, "advancedsearch": IA_SEARCH})(
        "ia", {"collection": "prelinger", **options}
    )
    source.limits = LimitsConfig(max_filesize_mb=options.pop("_cap", 200))
    return source


def test_archive_requires_a_recognised_licence():
    items = _archive().discover()
    # Most Archive items carry no licence at all; unlicensed and NC are dropped.
    assert [i.id for i in items] == ["Cheerios1960"]


def test_archive_query_forces_the_licence_filter():
    assert "licenseurl:[* TO *]" in _archive()._query()
    assert 'mediatype:"movies"' in _archive()._query()


def test_archive_query_can_opt_out_of_the_licence_filter():
    assert "licenseurl" not in _archive(require_license=False)._query()


@pytest.mark.parametrize(
    ("prefer", "cap_mb", "expected"),
    [
        ("smallest", 200, "c_512kb.mp4"),
        ("largest", 200, "c.mp4"),
        # Nothing fits: report the smallest so the limits filter can reject it
        # with a real size instead of a guess.
        ("largest", 1, "c_512kb.mp4"),
    ],
)
def test_archive_picks_a_derivative_that_fits_the_disk_budget(prefer, cap_mb, expected):
    source = _stub(ArchiveOrgSource, {"/metadata/": IA_METADATA, "advancedsearch": IA_SEARCH})(
        "ia", {"collection": "prelinger", "prefer": prefer}
    )
    source.limits = LimitsConfig(max_filesize_mb=cap_mb)
    assert source.discover()[0].extra["file"] == expected


def test_archive_item_fields():
    item = _archive().discover()[0]
    assert item.license == "publicdomain"
    assert item.duration == 60
    assert item.description == "A cereal ad."
    assert item.fetch_strategy == FETCH_DIRECT
    assert item.download_url.startswith("https://archive.org/download/Cheerios1960/")
    assert item.webpage_url == "https://archive.org/details/Cheerios1960"


# --------------------------------------------------------------------------
# Wikimedia Commons

WM_PAGES = {
    "query": {
        "pages": [
            {
                "pageid": 123,
                "title": "File:Black Holes (NASA).ogv",
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/wikipedia/commons/c/c2/Black.ogv",
                        "thumburl": "https://upload.wikimedia.org/thumb/1280px-Black.ogv.jpg",
                        "size": 114_329_124,
                        "duration": 246.56,
                        "mime": "application/ogg",
                        "user": "Uploader",
                        "extmetadata": {
                            "ImageDescription": {"value": "<p>A film about <i>black holes</i>.</p>"},
                            "Artist": {"value": "<a href='#'>ScienceAtNASA</a>"},
                            "LicenseShortName": {"value": "CC BY 3.0"},
                            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/3.0"},
                        },
                    }
                ],
            },
            {
                "pageid": 124,
                "title": "File:Nonfree.webm",
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/x.webm",
                        "size": 1,
                        "extmetadata": {"LicenseShortName": {"value": "All rights reserved"}},
                    }
                ],
            },
        ]
    }
}


def test_wikimedia_filters_by_licence():
    source = _stub(WikimediaSource, {"api.php": WM_PAGES})("wm", {"search": "nasa"})
    assert [i.id for i in source.discover()] == ["123"]


def test_wikimedia_item_fields():
    source = _stub(WikimediaSource, {"api.php": WM_PAGES})("wm", {"search": "nasa"})
    item = source.discover()[0]

    assert item.title == "Black Holes (NASA)"
    assert item.author == "ScienceAtNASA"
    assert item.description == "A film about black holes."
    assert item.duration == 246
    assert item.license == "CC BY 3.0"
    assert item.webpage_url == "https://commons.wikimedia.org/wiki/File:Black_Holes_(NASA).ogv"
    assert item.fetch_strategy == FETCH_DIRECT


def test_wikimedia_requests_a_thumbnail_width():
    captured = {}

    class Recording(WikimediaSource):
        def get_json(self, url, params=None, timeout=30):
            captured.update(params or {})
            return WM_PAGES

    Recording("wm", {"search": "nasa"}).discover()
    # Without iiurlwidth the API returns no thumburl for video files.
    assert captured["iiurlwidth"]


# --------------------------------------------------------------------------
# NASA

NASA_SEARCH = {
    "collection": {
        "items": [
            {
                "href": "https://images-assets.nasa.gov/video/A B/collection.json",
                "data": [
                    {
                        "nasa_id": "A B",
                        "title": "Artemis II",
                        "description": "A mission.",
                        "date_created": "2026-01-02T00:00:00Z",
                        "keywords": ["Artemis", "Moon"],
                        "center": "HQ",
                    }
                ],
                "links": [
                    {
                        "render": "image",
                        "href": "https://images-assets.nasa.gov/video/A B/A B~large.jpg",
                    }
                ],
            }
        ]
    }
}
NASA_ASSETS = [
    "http://images-assets.nasa.gov/video/A B/A B~orig.mp4",
    "http://images-assets.nasa.gov/video/A B/A B~large.mp4",
    "http://images-assets.nasa.gov/video/A B/A B~medium.mp4",
    "http://images-assets.nasa.gov/video/A B/A B~large.jpg",
]


def _nasa(**options):
    return _stub(NasaSource, {"collection.json": NASA_ASSETS, "search": NASA_SEARCH})("nasa", options)


def test_nasa_percent_encodes_urls_containing_spaces():
    item = _nasa().discover()[0]
    assert " " not in item.download_url
    assert " " not in item.webpage_url
    assert " " not in item.thumbnail_url
    assert item.download_url.startswith("https://")


@pytest.mark.parametrize(
    ("quality", "expected"),
    [("large", "~large.mp4"), ("medium", "~medium.mp4"), ("orig", "~orig.mp4")],
)
def test_nasa_honours_the_requested_rendition(quality, expected):
    assert _nasa(quality=quality).discover()[0].download_url.endswith(expected)


def test_nasa_falls_back_when_the_requested_rendition_is_absent():
    # 'small' does not exist in the fixture; the next smaller-then-larger
    # rendition should be used rather than failing.
    assert _nasa(quality="small").discover()[0].download_url.endswith("~medium.mp4")


def test_nasa_asserts_public_domain_as_policy():
    item = _nasa().discover()[0]
    assert "public domain" in item.license.lower()
    assert item.author == "NASA/HQ"
