"""Low-level AcFun member API client.

Endpoint inventory and payload shapes were recovered from AcFun's own public
web bundle (``member.acfun.cn`` webpack chunks) and verified against the live
service. Two details differ from the long-abandoned ``acfun_upload`` PyPI
package and silently break it:

* the chunked upload gateway answers a ``python-requests`` User-Agent with an
  empty 200 for anything at all, storing nothing, so uploads must go out under
  the browser User-Agent and have their acknowledgement checked;
* covers no longer go to Qiniu. ``getQiniuToken`` keeps its legacy name but
  returns a Kuaishou endpoint list.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from ...errors import AccountBlockedError, AuthError, PublishError
from ...utils.http import build_session
from .auth import AcFunCredentials

log = logging.getLogger(__name__)

MEMBER_BASE = "https://member.acfun.cn"
UPLOAD_VIDEO_REFERER = f"{MEMBER_BASE}/upload-video"
POST_ARTICLE_REFERER = f"{MEMBER_BASE}/post-article"

#: Fallback chunked-upload host, used when the token response omits one.
DEFAULT_UPLOAD_HOST = "upload.kuaishouzt.com"

#: "抱歉，系统正在升级维护，该账号暂时无法投稿". Despite the wording it is scoped to
#: the account, not the platform, and it answers every submission endpoint --
#: ``postArticle`` and ``getKSCloudToken`` alike -- while the session stays
#: valid. Worth its own exception because no other item will fare better.
ACCOUNT_BLOCKED_RESULT = 109020


def _envelope_error(payload: Any) -> tuple[int, str] | None:
    """Return ``(result, message)`` if `payload` carries a rejection.

    Two shapes are in play: most endpoints answer with ``result`` at the top
    level, but ``getKSCloudToken`` nests the same fields under ``errMsg``, which
    used to slip past this check and surface downstream as a missing token.
    """
    if not isinstance(payload, dict):
        return None

    envelope = payload
    if isinstance(payload.get("errMsg"), dict):
        envelope = payload["errMsg"]

    result = envelope.get("result")
    if not isinstance(result, int) or result == 0:
        return None
    return result, str(envelope.get("error_msg") or envelope.get("errorMsg") or "")


class AcFunClient:
    """Authenticated wrapper around the member API.

    Every method raises `PublishError` on a non-zero ``result`` so callers do
    not have to re-check the envelope.
    """

    def __init__(
        self,
        credentials: AcFunCredentials,
        session: requests.Session | None = None,
        timeout: int = 60,
        upload_timeout: int = 300,
    ) -> None:
        self.credentials = credentials
        self.session = session or build_session()
        self.timeout = timeout
        self.upload_timeout = upload_timeout

    def headers(self, referer: str = UPLOAD_VIDEO_REFERER) -> dict[str, str]:
        """Browser-equivalent headers.

        ``Origin`` and ``Referer`` are mandatory: AcFun rejects member API
        calls that omit them, which is another reason the 2020 package fails.
        """
        return {
            "Cookie": self.credentials.header,
            "Origin": MEMBER_BASE,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        }

    def _request(
        self,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        referer: str = UPLOAD_VIDEO_REFERER,
        expect_envelope: bool = True,
    ) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{MEMBER_BASE}{path}"
        try:
            response = self.session.post(
                url,
                data=data,
                json=json_body,
                headers=self.headers(referer),
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise PublishError(f"AcFun request to {path} failed: {exc}") from exc

        # A redirect to the login page is how member.acfun.cn signals an
        # expired session; it never returns 401 for browser-shaped requests.
        if response.status_code in (301, 302, 303, 307, 308):
            raise AuthError(f"AcFun redirected {path} to login -- the session is no longer valid.")
        if response.status_code == 401:
            raise AuthError(f"AcFun returned 401 for {path} -- the session is no longer valid.")

        try:
            payload = response.json()
        except ValueError as exc:
            raise PublishError(
                f"AcFun returned non-JSON for {path} (HTTP {response.status_code}): {response.text[:200]}"
            ) from exc

        if expect_envelope:
            error = _envelope_error(payload)
            if error:
                result, message = error
                description = f"AcFun rejected {path}: result={result} {message}".strip()
                if result == ACCOUNT_BLOCKED_RESULT:
                    raise AccountBlockedError(description)
                raise PublishError(description)
        return payload

    def post_form(
        self, path: str, data: dict[str, Any], referer: str = UPLOAD_VIDEO_REFERER
    ) -> dict[str, Any]:
        """POST ``application/x-www-form-urlencoded`` (video and article APIs)."""
        return self._request(path, data=data, referer=referer)

    def post_json(
        self, path: str, payload: dict[str, Any], referer: str = UPLOAD_VIDEO_REFERER
    ) -> dict[str, Any]:
        """POST ``application/json`` (the image upload APIs expect this)."""
        return self._request(path, json_body=payload, referer=referer)

    # ---- Discovery -------------------------------------------------------

    def get_channel_list(self) -> list[dict[str, Any]]:
        """Return the combined channel tree.

        One unparameterised call yields both the video partition tree (nested
        under ``children``) and the article realm tree (under ``realms``).
        """
        payload = self._request(
            "/common/api/getChannelList",
            data={},
            referer=POST_ARTICLE_REFERER,
            expect_envelope=False,
        )
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("channelList", "data", "info"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        raise PublishError(f"Unexpected getChannelList response shape: {type(payload).__name__}")
