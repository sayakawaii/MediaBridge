from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mediabridge.errors import PublishError
from mediabridge.publishers.acfun.upload import (
    _redact,
    _require_ack,
    _upload_fragment,
    _upload_user_agent,
)
from mediabridge.publishers.acfun.video import TranscodeOutcome, verify_transcode


def test_redacts_upload_tokens_from_urls():
    message = (
        "HTTPSConnectionPool: Max retries exceeded with url: "
        "/api/upload/fragment?fragmentId=8&uploadToken=Cg51cGxvYWRlci50b2tlbhLTCPO6JESkRDKY"
    )
    redacted = _redact(message)
    # Actions logs are public on public repositories, and this is a live credential.
    assert "Cg51cGxvYWRlci50b2tlbhLTCPO6JESkRDKY" not in redacted
    assert "uploadToken=<redacted>" in redacted
    assert "fragmentId=8" in redacted


def test_redacts_the_snake_case_spelling_too():
    assert _redact("?upload_token=abc123&x=1") == "?upload_token=<redacted>&x=1"


def test_redaction_leaves_unrelated_text_alone():
    assert _redact("Fragment 3 failed: connection reset") == "Fragment 3 failed: connection reset"


def test_upload_user_agent_is_not_the_requests_default():
    # A python-requests User-Agent reaches a handler that answers everything
    # with an empty 200 and stores nothing, so uploads silently vanish.
    agent = _upload_user_agent()
    assert "python-requests/" not in agent
    assert agent.startswith("Mozilla/5.0")


def _reply(status: int = 200, *, content: bytes = b"", payload: object = None) -> SimpleNamespace:
    body = json.dumps(payload).encode() if payload is not None else content
    return SimpleNamespace(
        status_code=status,
        content=body,
        text=body.decode() or "",
        json=lambda: json.loads(body),
    )


def test_an_empty_body_is_a_failure_not_a_success():
    # The gateway returns an empty 200 when the write never reached storage.
    # Reading that as success is what let four days of uploads report
    # completion while sending the file nowhere.
    with pytest.raises(PublishError, match="empty body"):
        _require_ack(_reply(), "Fragment 0")


def test_a_rejected_write_is_reported():
    with pytest.raises(PublishError, match="rejected"):
        _require_ack(_reply(payload={"result": 0, "error_msg": "bad token"}), "Fragment 0")


def test_an_acknowledged_write_returns_the_body():
    assert _require_ack(_reply(payload={"result": 1, "size": 12}), "Fragment 0")["size"] == 12


def test_a_short_write_is_retried_then_reported(monkeypatch):
    monkeypatch.setattr("mediabridge.publishers.acfun.upload.time.sleep", lambda _s: None)
    # The gateway acknowledges the fragment but reports fewer bytes than were
    # sent, which is a truncated write rather than a transport error.
    session = SimpleNamespace(post=lambda *a, **k: _reply(payload={"result": 1, "size": 4}))
    with pytest.raises(PublishError, match="stored 4 bytes of 9"):
        _upload_fragment(
            session, "upload.example", "tok", b"123456789", index=0, start=0, total_size=9, timeout=1
        )


def test_fragments_are_numbered_with_the_snake_case_parameters():
    sent: dict = {}

    def post(url, params=None, data=None, headers=None, timeout=None):
        sent.update(params=params, headers=headers)
        return _reply(payload={"result": 1, "size": len(data)})

    _upload_fragment(
        SimpleNamespace(post=post), "upload.example", "tok", b"abc", index=3, start=6, total_size=9, timeout=1
    )
    assert sent["params"] == {"fragment_id": "3", "upload_token": "tok"}
    assert "python-requests/" not in sent["headers"]["User-Agent"]


def test_a_lasting_transcode_failure_is_reported(monkeypatch, caplog):
    monkeypatch.setattr("mediabridge.publishers.acfun.video.time.sleep", lambda _s: None)
    monkeypatch.setattr("mediabridge.publishers.acfun.video.VERIFY_TIMEOUT_SEC", 0.01)
    client = SimpleNamespace(post_form=lambda *a, **k: {"videoList": [{"sourceStatus": 2}]})
    with caplog.at_level("ERROR"):
        assert verify_transcode(client, "123") == TranscodeOutcome("转码失败", failed=True)
    assert "will not play" in caplog.text


def test_a_video_that_starts_transcoding_is_accepted():
    client = SimpleNamespace(post_form=lambda *a, **k: {"videoList": [{"sourceStatus": 3}]})
    assert verify_transcode(client, "123") == TranscodeOutcome("审核中")
