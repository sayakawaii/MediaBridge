from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mediabridge.errors import AuthError
from mediabridge.publishers.acfun.auth import (
    AcFunCredentials,
    load_credentials,
    parse_cookie_blob,
)

# The shape a browser's Set-Cookie response headers actually take, CRLF and all.
SET_COOKIE_DUMP = (
    "_did=web_1234567890ABCDEF;Domain=acfun.cn;Path=/;Expires=Sat, 29 Aug 2026 02:36:00 GMT\r\n"
    "acPasstoken=ChVhY2Z1bi5hcGkuc2VydmljZS50b2tlbhIwSECRET;Domain=.acfun.cn;Path=/;"
    "Expires=Sat, 29 Aug 2026 02:36:00 GMT;HttpOnly\r\n"
    "auth_key=100912;Domain=.acfun.cn;Path=/;Expires=Sat, 29 Aug 2026 02:36:00 GMT\r\n"
    "ac_username=tester;Domain=.acfun.cn;Path=/\r\n"
    "Hm_lvt_abcdef=1750000000;Domain=.acfun.cn;Path=/\r\n"
)

COOKIE_EDITOR_JSON = json.dumps(
    [
        {"name": "acPasstoken", "value": "SECRET", "expirationDate": 1787020560.0},
        {"name": "auth_key", "value": "100912", "expirationDate": 1787020560.0},
        {"name": "_did", "value": "web_1", "expirationDate": 1787020560.0},
        {"name": "_ga", "value": "GA1.2.3", "expirationDate": 1787020560.0},
    ]
)


def test_parses_set_cookie_dump():
    credentials = parse_cookie_blob(SET_COOKIE_DUMP)
    assert credentials.cookies["auth_key"] == "100912"
    assert credentials.cookies["acPasstoken"].startswith("ChVhY2Z1bi")
    assert credentials.user_id == "100912"


def test_drops_analytics_cookies():
    # These leak browsing history into the secret and are never needed.
    assert "Hm_lvt_abcdef" not in parse_cookie_blob(SET_COOKIE_DUMP).cookies
    assert "_ga" not in parse_cookie_blob(COOKIE_EDITOR_JSON).cookies


def test_reads_expiry_from_set_cookie_attributes():
    credentials = parse_cookie_blob(SET_COOKIE_DUMP)
    assert credentials.expires_at == datetime(2026, 8, 29, 2, 36, tzinfo=timezone.utc)


def test_parses_cookie_editor_json():
    credentials = parse_cookie_blob(COOKIE_EDITOR_JSON)
    assert credentials.cookies["auth_key"] == "100912"
    assert credentials.expires_at is not None


def test_parses_plain_cookie_header():
    credentials = parse_cookie_blob("acPasstoken=abc; auth_key=100912; ac_username=x")
    assert credentials.cookies == {"acPasstoken": "abc", "auth_key": "100912", "ac_username": "x"}


def test_header_is_rendered_for_requests():
    header = parse_cookie_blob("acPasstoken=abc; auth_key=1").header
    assert header == "acPasstoken=abc; auth_key=1"


def test_rejects_blob_without_session_cookies():
    with pytest.raises(AuthError, match="missing required cookie"):
        parse_cookie_blob("ac_username=tester; _did=web_1")


def test_rejects_empty_blob():
    with pytest.raises(AuthError, match="empty"):
        parse_cookie_blob("   ")


def test_rejects_malformed_json():
    with pytest.raises(AuthError, match="does not parse"):
        parse_cookie_blob('[{"name": "acPasstoken"')


def test_days_remaining():
    soon = datetime.now(timezone.utc) + timedelta(days=3)
    credentials = AcFunCredentials(cookies={"auth_key": "1"}, expires_at=soon)
    assert 2.9 < credentials.days_remaining < 3.1


def test_near_expiry_warns_but_does_not_raise(caplog):
    soon = datetime.now(timezone.utc) + timedelta(days=2)
    credentials = AcFunCredentials(cookies={"auth_key": "1"}, expires_at=soon)
    with caplog.at_level("WARNING"):
        credentials.check_expiry()
    # The cookie's stated expiry is advisory; only `verify` is authoritative.
    assert "expires in" in caplog.text


def test_environment_variable_takes_priority(monkeypatch, tmp_path):
    path = tmp_path / "ac_cookie.txt"
    path.write_text("acPasstoken=fromfile; auth_key=1", encoding="utf-8")
    monkeypatch.setenv("ACFUN_COOKIE", "acPasstoken=fromenv; auth_key=2")

    credentials = load_credentials(cookie_env="ACFUN_COOKIE", cookie_file=str(path))
    assert credentials.cookies["acPasstoken"] == "fromenv"


def test_falls_back_to_cookie_file(monkeypatch, tmp_path):
    monkeypatch.delenv("ACFUN_COOKIE", raising=False)
    path = tmp_path / "ac_cookie.txt"
    path.write_text(SET_COOKIE_DUMP, encoding="utf-8")

    credentials = load_credentials(cookie_env="ACFUN_COOKIE", cookie_file=str(path))
    assert credentials.user_id == "100912"


def test_missing_credentials_are_reported(monkeypatch, tmp_path):
    monkeypatch.delenv("ACFUN_COOKIE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(AuthError, match="No AcFun credentials"):
        load_credentials(cookie_env="ACFUN_COOKIE", cookie_file=str(tmp_path / "absent.txt"))
