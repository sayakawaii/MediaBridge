"""Source abstraction.

A source discovers candidate items and normalises them into `MediaItem`. It
must not download media, publish anything, or care which platform consumes its
output.

Implementing a source means subclassing `Source`, declaring an options schema,
and yielding `MediaItem`s from `discover`. See docs/EXTENDING.md.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import requests
from pydantic import BaseModel, ValidationError

from ..errors import ConfigError, SourceError
from ..models import MediaItem
from ..utils.http import build_session

log = logging.getLogger(__name__)


class SourceOptions(BaseModel):
    """Base class for per-source option schemas."""

    limit: int = 10
    """Maximum number of candidates to return from one discovery pass."""


class Source(ABC):
    """Discovers repostable items from one upstream service."""

    type_name: ClassVar[str] = ""
    options_model: ClassVar[type[SourceOptions]] = SourceOptions

    #: Human-readable note shown in `mediabridge sources`, e.g. licence caveats.
    description: ClassVar[str] = ""

    def __init__(self, name: str, options: dict[str, Any], session: requests.Session | None = None) -> None:
        self.name = name
        self.session = session or build_session()

        #: Global limits, injected by the orchestrator before `discover`.
        #: Sources that choose between several renditions (Internet Archive,
        #: NASA) consult this so they pick one that will actually fit on disk.
        self.limits: Any | None = None

        try:
            self.options = self.options_model.model_validate(options or {})
        except ValidationError as exc:
            raise ConfigError(f"Invalid options for source '{name}' (type={self.type_name}):\n{exc}") from exc

    @property
    def max_bytes(self) -> int | None:
        limit_mb = getattr(self.limits, "max_filesize_mb", None)
        return int(limit_mb) * 1048576 if limit_mb else None

    @abstractmethod
    def discover(self) -> list[MediaItem]:
        """Return candidate items, newest first where the API allows it."""

    # ---- Helpers for subclasses -----------------------------------------

    def get_json(self, url: str, params: dict[str, Any] | None = None, timeout: int = 30) -> Any:
        try:
            response = self.session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise SourceError(f"[{self.name}] request to {url} failed: {exc}") from exc
        except ValueError as exc:
            raise SourceError(f"[{self.name}] {url} returned invalid JSON: {exc}") from exc

    def make_item(self, **kwargs: Any) -> MediaItem:
        kwargs.setdefault("source_name", self.name)
        kwargs.setdefault("source_type", self.type_name)
        return MediaItem(**kwargs)
