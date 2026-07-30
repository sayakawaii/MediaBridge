"""Shared HTTP session factory."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def build_session(
    *,
    user_agent: str = USER_AGENT,
    total_retries: int = 3,
    backoff_factor: float = 1.0,
) -> requests.Session:
    """A session that retries idempotent failures with exponential backoff.

    POST is included in `allowed_methods` because every AcFun API call is a
    POST, and the retried statuses (429/5xx) mean the request never reached
    application logic.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept": "*/*"})

    retry = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
