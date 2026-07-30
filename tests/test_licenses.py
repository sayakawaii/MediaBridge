from __future__ import annotations

import pytest

from mediabridge.filters import licenses


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://creativecommons.org/publicdomain/zero/1.0/", "cc0"),
        ("https://creativecommons.org/publicdomain/mark/1.0/", "pdm"),
        # The legacy form still dominates Internet Archive metadata.
        ("http://creativecommons.org/licenses/publicdomain/", "publicdomain"),
        ("http://www.usa.gov/publicdomain/label/1.0/", "us-gov"),
        ("https://creativecommons.org/licenses/by/4.0/", "cc-by"),
        ("https://creativecommons.org/licenses/by-sa/3.0", "cc-by-sa"),
        ("https://creativecommons.org/licenses/by-nd/4.0/", "cc-by-nd"),
        ("https://creativecommons.org/licenses/by-nc/4.0/", "cc-by-nc"),
        ("https://creativecommons.org/licenses/by-nc-sa/4.0/", "cc-by-nc-sa"),
        ("https://creativecommons.org/licenses/by-nc-nd/4.0/", "cc-by-nc-nd"),
        ("https://example.com/terms", ""),
    ],
)
def test_identifies_licence_urls(url, expected):
    assert licenses.identify(url=url) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("CC BY 3.0", "cc-by"),
        ("cc-by-sa-4.0", "cc-by-sa"),
        ("Attribution-ShareAlike", "cc-by-sa"),
        ("Attribution-NonCommercial-NoDerivs", "cc-by-nc-nd"),
        ("Public Domain Dedication", "cc0"),
        ("public domain", "publicdomain"),
        ("Free of known copyright restrictions", "no-known-restrictions"),
        ("All rights reserved", ""),
        ("", ""),
    ],
)
def test_identifies_licence_names(name, expected):
    assert licenses.identify(name=name) == expected


def test_url_wins_over_name():
    # The name is free text; the URL is canonical.
    assert licenses.identify("All rights reserved", "https://creativecommons.org/licenses/by/4.0/") == "cc-by"


def test_unknown_licence_is_not_allowed():
    # An unrecognised licence must never be treated as "probably fine".
    assert licenses.is_allowed("Some bespoke terms", "https://example.com/x") is False
    assert licenses.is_allowed("", "") is False


def test_noncommercial_is_excluded_by_default():
    # AcFun runs monetisation programmes, so NC reposting is not safe by default.
    assert licenses.is_allowed(url="https://creativecommons.org/licenses/by-nc/4.0/") is False


def test_noderivatives_is_allowed_by_default():
    # A verbatim repost creates no derivative work.
    assert licenses.is_allowed(url="https://creativecommons.org/licenses/by-nd/4.0/") is True


def test_allowlist_groups_expand():
    allowed = licenses.parse_allowlist(["non-commercial"])
    assert "cc-by-nc-sa" in allowed
    assert licenses.is_allowed(url="https://creativecommons.org/licenses/by-nc/4.0/", allowed=allowed)
    assert not licenses.is_allowed(url="https://creativecommons.org/licenses/by/4.0/", allowed=allowed)


def test_allowlist_accepts_explicit_identifiers():
    allowed = licenses.parse_allowlist(["cc0"])
    assert allowed == {"cc0"}


def test_empty_allowlist_falls_back_to_default():
    assert licenses.parse_allowlist([]) == licenses.DEFAULT_ALLOWED
    assert licenses.parse_allowlist(None) == licenses.DEFAULT_ALLOWED
