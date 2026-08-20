"""Exception hierarchy.

Every failure mode that a user can realistically fix on their own gets its own
class so the CLI can print an actionable hint instead of a bare traceback.
"""

from __future__ import annotations


class MediaBridgeError(Exception):
    """Base class for all MediaBridge failures."""

    hint: str = ""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        if hint:
            self.hint = hint


class ConfigError(MediaBridgeError):
    """Configuration file is missing, malformed, or fails validation."""


class AuthError(MediaBridgeError):
    """AcFun credentials are missing, malformed, or no longer valid."""

    hint = (
        "Re-export your AcFun cookies from a logged-in browser and update the "
        "ACFUN_COOKIE secret. See docs/COOKIES.md."
    )


class SourceError(MediaBridgeError):
    """A source adapter could not list candidate items."""


class FetchError(MediaBridgeError):
    """Downloading the media file or its cover failed."""


class PublishError(MediaBridgeError):
    """The upstream platform rejected the submission."""


class AccountBlockedError(PublishError):
    """AcFun is refusing submissions from this account, whatever the item.

    Distinct from `AuthError`: the session is perfectly valid -- ``login-check``
    passes and returns a uid -- but every submission endpoint answers
    ``result=109020``. Trying the next item cannot help, so this aborts the run
    instead of grinding through every source to collect the same refusal.
    """

    hint = (
        "The session is valid, so a new cookie will not help. Sign in to AcFun "
        "and try submitting something by hand: if that is refused too, the "
        "account itself is restricted and only AcFun can lift it."
    )


class SkipItem(MediaBridgeError):
    """Not an error: the item is deliberately not published this run.

    Raised by filters and fetchers to unwind out of a single item's pipeline
    without aborting the whole run.
    """
