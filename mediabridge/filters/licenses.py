"""Licence recognition and the default redistribution whitelist.

MediaBridge reposts works verbatim, so the only licences safe to enable by
default are those that permit unmodified redistribution by anyone.

NonCommercial variants are excluded from the default whitelist on purpose:
AcFun runs monetisation programmes (香蕉 rewards, creator incentives), which
makes NC reposting arguable at best. An operator who has decided otherwise can
opt in per source. NoDerivatives *is* allowed, because a verbatim repost
creates no derivative work.
"""

from __future__ import annotations

import re

# Canonical identifiers, normalised to lowercase without punctuation noise.
PUBLIC_DOMAIN = frozenset({"cc0", "publicdomain", "pdm", "us-gov", "no-known-restrictions"})
ATTRIBUTION = frozenset({"cc-by", "cc-by-sa", "cc-by-nd"})
NON_COMMERCIAL = frozenset({"cc-by-nc", "cc-by-nc-sa", "cc-by-nc-nd"})

DEFAULT_ALLOWED = PUBLIC_DOMAIN | ATTRIBUTION

_URL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"creativecommons\.org/publicdomain/zero"), "cc0"),
    (re.compile(r"creativecommons\.org/publicdomain/mark"), "pdm"),
    # The legacy /licenses/publicdomain/ form predates the /publicdomain/
    # namespace and is still the most common public-domain marker on
    # Internet Archive items, so it has to be matched before /licenses/by.
    (re.compile(r"creativecommons\.org/(licenses/)?publicdomain"), "publicdomain"),
    (re.compile(r"usa\.gov/publicdomain"), "us-gov"),
    (re.compile(r"creativecommons\.org/licenses/by-nc-nd"), "cc-by-nc-nd"),
    (re.compile(r"creativecommons\.org/licenses/by-nc-sa"), "cc-by-nc-sa"),
    (re.compile(r"creativecommons\.org/licenses/by-nc"), "cc-by-nc"),
    (re.compile(r"creativecommons\.org/licenses/by-nd"), "cc-by-nd"),
    (re.compile(r"creativecommons\.org/licenses/by-sa"), "cc-by-sa"),
    (re.compile(r"creativecommons\.org/licenses/by"), "cc-by"),
]

_NAME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bcc0\b|public\s*domain\s*dedication"), "cc0"),
    (re.compile(r"public\s*domain\s*mark"), "pdm"),
    (re.compile(r"public\s*domain"), "publicdomain"),
    (re.compile(r"free\s+of\s+known\s+copyright"), "no-known-restrictions"),
    (re.compile(r"attribution.*non.?commercial.*no.?deriv"), "cc-by-nc-nd"),
    (re.compile(r"attribution.*non.?commercial.*share.?alike"), "cc-by-nc-sa"),
    (re.compile(r"attribution.*non.?commercial"), "cc-by-nc"),
    (re.compile(r"attribution.*no.?deriv"), "cc-by-nd"),
    (re.compile(r"attribution.*share.?alike"), "cc-by-sa"),
    (re.compile(r"\bcc[\s-]*by[\s-]*nc[\s-]*nd\b"), "cc-by-nc-nd"),
    (re.compile(r"\bcc[\s-]*by[\s-]*nc[\s-]*sa\b"), "cc-by-nc-sa"),
    (re.compile(r"\bcc[\s-]*by[\s-]*nc\b"), "cc-by-nc"),
    (re.compile(r"\bcc[\s-]*by[\s-]*nd\b"), "cc-by-nd"),
    (re.compile(r"\bcc[\s-]*by[\s-]*sa\b"), "cc-by-sa"),
    (re.compile(r"\bcc[\s-]*by\b|^attribution$"), "cc-by"),
]


def identify(name: str = "", url: str = "") -> str:
    """Reduce a free-text licence name and/or URL to a canonical identifier.

    Returns an empty string when nothing recognisable is present, which the
    caller must treat as "not permitted" rather than "probably fine".
    """
    haystack_url = (url or "").lower()
    for pattern, ident in _URL_PATTERNS:
        if pattern.search(haystack_url):
            return ident

    haystack_name = re.sub(r"[_]+", " ", (name or "").lower())
    for pattern, ident in _NAME_PATTERNS:
        if pattern.search(haystack_name):
            return ident

    return ""


def is_allowed(name: str = "", url: str = "", allowed: frozenset[str] | set[str] | None = None) -> bool:
    allowed = DEFAULT_ALLOWED if allowed is None else allowed
    ident = identify(name, url)
    return bool(ident) and ident in allowed


def parse_allowlist(values: list[str] | None) -> frozenset[str]:
    """Turn config strings into canonical identifiers.

    Accepts the shorthand groups ``public-domain``, ``attribution`` and
    ``non-commercial`` alongside explicit identifiers such as ``cc-by-sa``.
    """
    if not values:
        return DEFAULT_ALLOWED

    groups = {
        "public-domain": PUBLIC_DOMAIN,
        "attribution": ATTRIBUTION,
        "non-commercial": NON_COMMERCIAL,
        "all": PUBLIC_DOMAIN | ATTRIBUTION | NON_COMMERCIAL,
    }

    resolved: set[str] = set()
    for value in values:
        key = str(value).strip().lower()
        if key in groups:
            resolved |= groups[key]
        else:
            resolved.add(key)
    return frozenset(resolved)
