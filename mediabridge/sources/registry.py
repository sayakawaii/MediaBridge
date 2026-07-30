"""Source discovery via entry points.

Built-in sources are registered through the very same
``mediabridge.sources`` entry-point group that third-party packages use, so an
external plugin is never a second-class citizen. The dotted fallback table
below only exists so the package still works when run from a source checkout
that has not been ``pip install``ed.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from ..errors import ConfigError
from .base import Source

log = logging.getLogger(__name__)

_BUILTIN: dict[str, str] = {
    "peertube": "mediabridge.sources.peertube:PeerTubeSource",
    "archive_org": "mediabridge.sources.archive_org:ArchiveOrgSource",
    "wikimedia": "mediabridge.sources.wikimedia:WikimediaSource",
    "nasa": "mediabridge.sources.nasa:NasaSource",
    "horizon": "mediabridge.sources.horizon:HorizonSource",
}


def _load_dotted(spec: str) -> type[Source]:
    module_name, _, attr = spec.partition(":")
    module = __import__(module_name, fromlist=[attr])
    return getattr(module, attr)


def available_types() -> dict[str, str]:
    """Map every registered source type to a short description."""
    found: dict[str, str] = {}

    for name, spec in _BUILTIN.items():
        try:
            found[name] = _load_dotted(spec).description
        except Exception as exc:  # noqa: BLE001
            log.debug("Built-in source %s unavailable: %s", name, exc)

    for entry in entry_points(group="mediabridge.sources"):
        if entry.name in found:
            continue
        try:
            found[entry.name] = entry.load().description
        except Exception as exc:  # noqa: BLE001
            log.warning("Source plugin '%s' failed to load: %s", entry.name, exc)

    return found


def get_source_class(type_name: str) -> type[Source]:
    """Resolve a source type, preferring installed entry points over built-ins."""
    for entry in entry_points(group="mediabridge.sources"):
        if entry.name == type_name:
            try:
                return entry.load()
            except Exception as exc:  # noqa: BLE001 - a broken plugin must name itself
                raise ConfigError(f"Source plugin '{type_name}' failed to load: {exc}") from exc

    if type_name in _BUILTIN:
        return _load_dotted(_BUILTIN[type_name])

    raise ConfigError(f"Unknown source type '{type_name}'. Available: {', '.join(sorted(available_types()))}")


def build_source(name: str, type_name: str, options: dict, session=None) -> Source:
    return get_source_class(type_name)(name, options, session=session)
