"""Publisher abstraction and registry.

A publisher consumes `FetchedMedia` and knows nothing about where the item was
discovered. Adding a new destination platform means implementing this
interface and registering it under the ``mediabridge.publishers`` entry-point
group -- no changes to the orchestrator.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from ..errors import ConfigError, PublishError
from ..models import FetchedMedia, PublishResult

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config, PublishConfig
    from .acfun.client import AcFunClient

log = logging.getLogger(__name__)


@dataclass
class PublishContext:
    """Shared runtime handed to every publisher."""

    config: Config
    dry_run: bool = False
    _acfun: AcFunClient | None = None

    def acfun(self) -> AcFunClient:
        """Build the AcFun client on first use.

        Deferring this keeps `mediabridge run --dry-run` usable without any
        credentials configured.
        """
        if self._acfun is None:
            from ..utils.http import build_session
            from .acfun.auth import load_credentials
            from .acfun.client import AcFunClient

            credentials = load_credentials(
                cookie_env=self.config.acfun.cookie_env,
                cookie_file=self.config.acfun.cookie_file,
            )
            credentials.check_expiry()
            self._acfun = AcFunClient(
                credentials,
                session=build_session(),
                timeout=self.config.acfun.request_timeout,
                upload_timeout=self.config.acfun.upload_timeout,
            )
        return self._acfun


class Publisher(ABC):
    """Uploads one prepared item to a destination platform."""

    type_name: ClassVar[str] = ""

    #: Set to False for publishers that consume inline payloads (articles).
    requires_media_file: ClassVar[bool] = True

    def __init__(self, ctx: PublishContext) -> None:
        self.ctx = ctx

    @abstractmethod
    def publish(self, fetched: FetchedMedia, publish_config: PublishConfig) -> PublishResult:
        """Submit the item, or describe the submission when running dry."""

    def validate_config(self, publish_config: PublishConfig) -> None:  # noqa: B027
        """Fail fast on bad targeting before anything is downloaded.

        Optional: a publisher with nothing to check should not be forced to
        write an empty override.
        """

    def republish(
        self, fetched: FetchedMedia, publish_config: PublishConfig, remote_id: str
    ) -> PublishResult:
        """Re-submit an item already published as `remote_id`.

        Only meaningful where the platform can revise a submission in place.
        AcFun can do this for articles but not for video, so the default is to
        refuse rather than to quietly post a duplicate.
        """
        raise PublishError(f"{self.type_name} cannot update an existing submission.")


_BUILTIN_PUBLISHERS: dict[str, str] = {
    "acfun_video": "mediabridge.publishers.acfun.video:AcFunVideoPublisher",
    "acfun_article": "mediabridge.publishers.acfun.article:AcFunArticlePublisher",
}


def _load_dotted(spec: str) -> type[Publisher]:
    module_name, _, attr = spec.partition(":")
    module = __import__(module_name, fromlist=[attr])
    return getattr(module, attr)


def get_publisher_class(type_name: str) -> type[Publisher]:
    """Resolve a publisher by name, preferring installed entry points."""
    from importlib.metadata import entry_points

    for entry in entry_points(group="mediabridge.publishers"):
        if entry.name == type_name:
            try:
                return entry.load()
            except Exception as exc:  # noqa: BLE001 - a broken plugin must name itself
                raise ConfigError(f"Publisher plugin '{type_name}' failed to load: {exc}") from exc

    if type_name in _BUILTIN_PUBLISHERS:
        return _load_dotted(_BUILTIN_PUBLISHERS[type_name])

    known = sorted(_BUILTIN_PUBLISHERS)
    raise ConfigError(f"Unknown publish target '{type_name}'. Available: {', '.join(known)}")


def build_publisher(type_name: str, ctx: PublishContext) -> Publisher:
    return get_publisher_class(type_name)(ctx)
