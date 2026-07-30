"""AcFun credential loading and validation.

AcFun issues no API tokens, so the only workable unattended credential is a
browser session cookie. Password login is deliberately not implemented: AcFun
serves a 4-character captcha at ``/rest/web/login/captcha`` once the same
account logs in repeatedly, which a scheduled job does by definition.

Two export formats are accepted so users can pick whichever is less painful:
the raw ``Set-Cookie`` response headers, or the JSON array produced by the
Cookie-Editor browser extension.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from ...errors import AuthError

log = logging.getLogger(__name__)

PERSONAL_INFO_URL = "https://www.acfun.cn/rest/pc-direct/user/personalInfo"

# Without both of these the session is anonymous; everything else is optional.
REQUIRED_COOKIES = ("acPasstoken", "auth_key")

# Analytics cookies that add nothing but leak browsing history into the secret.
_DROP_PREFIXES = ("Hm_lvt_", "Hm_lpvt_", "HMACCOUNT", "_ga", "_gid")

EXPIRY_WARN_DAYS = 7

_COOKIE_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.-]*)=([^;]*)")
_EXPIRES_ATTR = re.compile(r";\s*Expires=([^;]+)", re.IGNORECASE)
_MAX_AGE_ATTR = re.compile(r";\s*Max-Age=(\d+)", re.IGNORECASE)


@dataclass
class AcFunCredentials:
    """A validated set of AcFun session cookies."""

    cookies: dict[str, str] = field(default_factory=dict)
    expires_at: datetime | None = None
    source: str = "unknown"

    @property
    def header(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self.cookies.items())

    @property
    def user_id(self) -> str:
        return self.cookies.get("auth_key", "")

    @property
    def days_remaining(self) -> float | None:
        if self.expires_at is None:
            return None
        delta = self.expires_at - datetime.now(timezone.utc)
        return delta.total_seconds() / 86400

    def check_expiry(self) -> None:
        """Warn (never fail) when the session is close to lapsing.

        Failing here would be wrong: the cookie's stated expiry is only a hint,
        and AcFun can invalidate a session earlier or honour it for longer.
        `verify` is the authority on whether we are actually logged in.
        """
        days = self.days_remaining
        if days is None:
            return
        if days <= 0:
            log.warning("AcFun cookie expired %.1f days ago; a refresh is almost certainly needed.", -days)
        elif days <= EXPIRY_WARN_DAYS:
            log.warning(
                "AcFun cookie expires in %.1f days (%s). Re-export it soon -- see docs/COOKIES.md.",
                days,
                self.expires_at.strftime("%Y-%m-%d") if self.expires_at else "?",
            )
        else:
            log.info("AcFun cookie valid for another %.0f days.", days)


def _keep(name: str) -> bool:
    return not any(name.startswith(prefix) for prefix in _DROP_PREFIXES)


def _parse_set_cookie_dump(text: str) -> tuple[dict[str, str], datetime | None]:
    """Parse raw ``Set-Cookie`` header lines, one cookie per line."""
    cookies: dict[str, str] = {}
    expiries: list[datetime] = []

    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.lower().startswith("set-cookie:"):
            line = line.split(":", 1)[1].strip()

        match = _COOKIE_LINE.match(line)
        if not match:
            continue
        name, value = match.group(1), match.group(2).strip()
        if not _keep(name):
            continue
        cookies[name] = value

        if name in REQUIRED_COOKIES:
            expires = _EXPIRES_ATTR.search(line)
            if expires:
                try:
                    expiries.append(parsedate_to_datetime(expires.group(1).strip()))
                except (TypeError, ValueError):
                    pass
            else:
                max_age = _MAX_AGE_ATTR.search(line)
                if max_age:
                    expiries.append(
                        datetime.now(timezone.utc).replace(microsecond=0)
                        + timedelta(seconds=int(max_age.group(1)))
                    )

    return cookies, (min(expiries) if expiries else None)


def _parse_cookie_editor_json(data: object) -> tuple[dict[str, str], datetime | None]:
    """Parse the Cookie-Editor extension's JSON export, or a plain name/value map."""
    cookies: dict[str, str] = {}
    expiries: list[datetime] = []

    if isinstance(data, dict):
        for name, value in data.items():
            if _keep(str(name)):
                cookies[str(name)] = str(value)
        return cookies, None

    if not isinstance(data, list):
        raise AuthError("Cookie JSON must be an array of cookie objects or a name/value mapping.")

    for entry in data:
        if not isinstance(entry, dict) or "name" not in entry:
            continue
        name = str(entry["name"])
        if not _keep(name):
            continue
        cookies[name] = str(entry.get("value", ""))

        stamp = entry.get("expirationDate") or entry.get("expires")
        if name in REQUIRED_COOKIES and isinstance(stamp, (int, float)) and stamp > 0:
            expiries.append(datetime.fromtimestamp(float(stamp), tz=timezone.utc))

    return cookies, (min(expiries) if expiries else None)


def _parse_cookie_header(text: str) -> tuple[dict[str, str], datetime | None]:
    """Parse a single-line ``a=1; b=2`` request-header style string."""
    cookies: dict[str, str] = {}
    for part in text.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name and _keep(name):
            cookies[name] = value.strip()
    return cookies, None


def parse_cookie_blob(text: str, source: str = "unknown") -> AcFunCredentials:
    """Detect the export format and turn it into `AcFunCredentials`."""
    text = (text or "").strip()
    if not text:
        raise AuthError("AcFun cookie blob is empty.")

    if text[0] in "[{":
        try:
            cookies, expires = _parse_cookie_editor_json(json.loads(text))
        except json.JSONDecodeError as exc:
            raise AuthError(f"Cookie blob looks like JSON but does not parse: {exc}") from exc
    elif "\n" in text or "Domain=" in text or "Path=" in text:
        cookies, expires = _parse_set_cookie_dump(text)
    else:
        cookies, expires = _parse_cookie_header(text)

    missing = [name for name in REQUIRED_COOKIES if not cookies.get(name)]
    if missing:
        raise AuthError(
            f"AcFun cookie blob is missing required cookie(s): {', '.join(missing)}. "
            f"Found: {', '.join(sorted(cookies)) or '(none)'}"
        )

    return AcFunCredentials(cookies=cookies, expires_at=expires, source=source)


def load_credentials(cookie_env: str = "ACFUN_COOKIE", cookie_file: str | None = None) -> AcFunCredentials:
    """Load credentials from the environment, falling back to a local file."""
    raw = os.environ.get(cookie_env)
    if raw and raw.strip():
        return parse_cookie_blob(raw, source=f"${cookie_env}")

    candidates = [cookie_file] if cookie_file else []
    candidates += ["~/.mediabridge/ac_cookie.txt", "~/.mediabridge/ac_cookies.json"]

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return parse_cookie_blob(path.read_text(encoding="utf-8"), source=str(path))

    raise AuthError(
        f"No AcFun credentials found: environment variable ${cookie_env} is unset and "
        f"no cookie file exists at {', '.join(str(Path(c).expanduser()) for c in candidates if c)}.",
        hint="Export cookies from a logged-in browser -- see docs/COOKIES.md.",
    )


def verify(session: requests.Session, credentials: AcFunCredentials, timeout: int = 30) -> dict:
    """Confirm the session is logged in, returning AcFun's account summary.

    This is the only authority on session validity; the cookie's own expiry
    attribute is just an advisory hint.
    """
    try:
        response = session.get(
            PERSONAL_INFO_URL,
            headers={"Cookie": credentials.header, "Referer": "https://www.acfun.cn/"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise AuthError(f"Could not reach AcFun to validate the session: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthError(
            f"AcFun returned a non-JSON response (HTTP {response.status_code}) when validating the session."
        ) from exc

    if payload.get("result") != 0:
        raise AuthError(
            f"AcFun rejected the stored cookies: {payload.get('error_msg') or payload.get('result')}"
        )

    return payload.get("info", {}) or {}
