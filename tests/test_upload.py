from __future__ import annotations

from mediabridge.publishers.acfun.upload import _redact, _upload_user_agent


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


def test_upload_user_agent_keeps_the_requests_token():
    # The Kuaishou gateway rejects browser, curl and okhttp User-Agents with a
    # misleading "upload_token is not present"; only strings carrying
    # python-requests/<version> are accepted.
    agent = _upload_user_agent()
    assert "python-requests/" in agent
    assert agent.startswith("MediaBridge/")
