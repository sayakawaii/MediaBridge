"""Account-wide submission refusals, and upstream throttling.

Both exist because of a live incident. From 18 August every scheduled run went
red: AcFun answered `result=109020` -- "抱歉，系统正在升级维护，该账号暂时无法投稿"
-- to `postArticle` and `getKSCloudToken` alike, while `login-check` kept
passing and returning a uid. The run ground through all nine sources collecting
the same refusal, and the wall of per-item failures buried the one fact that
mattered. Separately, one Wikimedia file had been 429ing daily since 15 August,
which was enough on its own to fail runs that published everything else.
"""

from __future__ import annotations

import pytest

from mediabridge.config import Config
from mediabridge.errors import AccountBlockedError, PublishError, SkipItem
from mediabridge.fetchers.direct import download_to
from mediabridge.models import FetchedMedia, MediaItem, PublishResult
from mediabridge.orchestrator import Orchestrator
from mediabridge.publishers.acfun.auth import AcFunCredentials
from mediabridge.publishers.acfun.client import AcFunClient

BLOCKED_MESSAGE = "抱歉，系统正在升级维护，该账号暂时无法投稿"


# ---- Envelope detection --------------------------------------------------


class _Response:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self._status_code = status_code

    def post(self, url, **kwargs):
        return _Response(self._payload, self._status_code)


def build_client(payload, status_code: int = 200) -> AcFunClient:
    credentials = AcFunCredentials(cookies={"auth_key": "1"}, source="test")
    return AcFunClient(credentials, session=_Session(payload, status_code))


def test_flat_envelope_reports_the_account_block():
    """`postArticle` puts the refusal at the top level."""
    client = build_client({"result": 109020, "error_msg": BLOCKED_MESSAGE})

    with pytest.raises(AccountBlockedError) as excinfo:
        client.post_form("/article/api/postArticle", {})

    assert "109020" in str(excinfo.value)
    assert BLOCKED_MESSAGE in str(excinfo.value)


def test_nested_envelope_reports_the_account_block():
    """`getKSCloudToken` nests the same fields under `errMsg`.

    This shape used to slip past the envelope check entirely and surface as
    "AcFun did not return an upload token: {...}", which read like a protocol
    quirk rather than the account being barred.
    """
    client = build_client({"errMsg": {"result": 109020, "error_msg": BLOCKED_MESSAGE}, "isError": True})

    with pytest.raises(AccountBlockedError) as excinfo:
        client.post_form("/video/api/getKSCloudToken", {})

    assert "109020" in str(excinfo.value)


def test_nested_envelope_without_a_message_still_reports_the_block():
    """AcFun returned an empty `error_msg` on roughly half of these."""
    client = build_client({"errMsg": {"result": 109020, "error_msg": ""}, "isError": True})

    with pytest.raises(AccountBlockedError):
        client.post_form("/video/api/getKSCloudToken", {})


def test_other_rejections_stay_ordinary_publish_errors():
    """Only 109020 aborts the run; an over-long 简介 must not."""
    client = build_client({"result": 110014, "error_msg": "描述信息不能超过200个汉字"})

    with pytest.raises(PublishError) as excinfo:
        client.post_form("/article/api/postArticle", {})

    assert not isinstance(excinfo.value, AccountBlockedError)


def test_a_successful_envelope_is_returned_untouched():
    client = build_client({"result": 0, "taskId": "abc", "token": "xyz"})

    assert client.post_form("/video/api/getKSCloudToken", {})["taskId"] == "abc"


def test_the_block_carries_a_hint_that_a_new_cookie_will_not_help():
    """The operator's first instinct is to replace the token; it is wrong."""
    error = AccountBlockedError("AcFun rejected /article/api/postArticle: result=109020")

    assert "cookie" in error.hint.lower()
    assert "by hand" in error.hint


# ---- Aborting the run ----------------------------------------------------


class _FakeSource:
    def __init__(self, name: str, count: int = 3) -> None:
        self.name = name
        self.count = count
        self.limits = None

    def discover(self) -> list[MediaItem]:
        return [
            MediaItem(
                source_name=self.name,
                source_type="fake",
                id=f"{self.name}-{i}",
                title=f"{self.name} item {i}",
                webpage_url=f"https://example.org/{self.name}/{i}",
            )
            for i in range(self.count)
        ]


class _BlockedPublisher:
    """Refuses everything the way a barred account does."""

    def __init__(self, target: str) -> None:
        self.target = target
        self.attempts = 0

    def validate_config(self, publish_config) -> None:
        pass

    def publish(self, fetched, publish_config) -> PublishResult:
        self.attempts += 1
        raise AccountBlockedError(f"AcFun rejected /article/api/postArticle: result=109020 {BLOCKED_MESSAGE}")


def build_config(tmp_path, sources: list[tuple[str, str]], **limits) -> Config:
    limits.setdefault("max_items_per_source", 1)
    return Config.model_validate(
        {
            "version": 1,
            "state_file": str(tmp_path / "state.json"),
            "work_dir": str(tmp_path / "work"),
            "limits": limits,
            "sources": [
                {
                    "name": name,
                    "type": "fake",
                    "options": {},
                    "publish": {"target": target, "channel_id": 1, "realm_id": 1},
                }
                for name, target in sources
            ],
        }
    )


@pytest.fixture(autouse=True)
def no_downloads(monkeypatch):
    """Keep the real publish path but never touch the network.

    A dry run would skip fetching too, but it also skips recording, and one of
    these tests is precisely about what does not reach the ledger.
    """
    monkeypatch.setattr(
        "mediabridge.orchestrator.fetch", lambda item, work_dir, limits: FetchedMedia(item=item)
    )


@pytest.fixture
def blocked_wiring(monkeypatch):
    """Every source discovers; every publish is refused account-wide."""
    discovered: list[str] = []
    publisher = _BlockedPublisher("acfun_video")

    def fake_build_source(name, type_name, options, session=None):
        discovered.append(name)
        return _FakeSource(name)

    monkeypatch.setattr("mediabridge.orchestrator.build_source", fake_build_source)
    monkeypatch.setattr("mediabridge.orchestrator.build_publisher", lambda target, ctx: publisher)
    return discovered, publisher


def test_the_first_refusal_stops_the_whole_run(tmp_path, blocked_wiring):
    """Nine sources used to collect nine identical refusals over five minutes."""
    discovered, publisher = blocked_wiring
    config = build_config(
        tmp_path,
        [("video-a", "acfun_video"), ("video-b", "acfun_video"), ("article-a", "acfun_article")],
        max_items_per_target={"acfun_video": 4, "acfun_article": 3},
        max_items_per_run=7,
    )

    report = Orchestrator(config).run()

    assert discovered == ["video-a"], "later sources must not even be asked to discover"
    assert publisher.attempts == 1, "no second item may be attempted"
    assert report.aborted
    assert "109020" in report.aborted


def test_the_blocked_item_is_not_counted_as_its_own_failure(tmp_path, blocked_wiring):
    """The item is fine; blaming it would hide the real cause and skew the cap."""
    _, _ = blocked_wiring
    config = build_config(tmp_path, [("video-a", "acfun_video")])

    report = Orchestrator(config).run()

    assert report.failed == 0
    assert report.failures == []
    assert report.published == 0


def test_an_aborted_run_still_fails_the_workflow(tmp_path, blocked_wiring):
    """Publishing nothing is normally fine, so `failed` alone would go green."""
    _, _ = blocked_wiring
    config = build_config(tmp_path, [("video-a", "acfun_video")])

    report = Orchestrator(config).run()

    assert report.exit_code == 1
    assert "aborted=" in report.summary()


def test_nothing_is_recorded_as_published_when_the_account_is_blocked(tmp_path, blocked_wiring):
    _, _ = blocked_wiring
    config = build_config(tmp_path, [("video-a", "acfun_video")])
    orchestrator = Orchestrator(config)

    orchestrator.run()

    assert orchestrator.state.pending_verification() == []
    assert not orchestrator.state.is_published("video-a-0")


def test_an_ordinary_rejection_does_not_abort_the_run(tmp_path, monkeypatch):
    """Only the account-wide refusal is worth giving up the run for."""
    discovered: list[str] = []

    class _FlakyPublisher:
        def validate_config(self, publish_config) -> None:
            pass

        def publish(self, fetched, publish_config) -> PublishResult:
            raise PublishError("AcFun rejected /article/api/postArticle: result=110014")

    def fake_build_source(name, type_name, options, session=None):
        discovered.append(name)
        return _FakeSource(name, count=1)

    monkeypatch.setattr("mediabridge.orchestrator.build_source", fake_build_source)
    monkeypatch.setattr("mediabridge.orchestrator.build_publisher", lambda target, ctx: _FlakyPublisher())
    config = build_config(tmp_path, [("video-a", "acfun_video"), ("video-b", "acfun_video")])

    report = Orchestrator(config).run()

    assert discovered == ["video-a", "video-b"]
    assert report.aborted == ""
    assert report.failed == 2


# ---- Upstream throttling -------------------------------------------------


class _ThrottledResponse:
    status_code = 429
    headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self):
        raise AssertionError("a 429 must be recognised before raise_for_status")


class _ThrottledSession:
    def get(self, url, **kwargs):
        return _ThrottledResponse()


def test_a_sustained_429_skips_the_item_rather_than_failing_it(tmp_path):
    """One throttled Wikimedia file used to turn an otherwise good run red."""
    with pytest.raises(SkipItem) as excinfo:
        download_to(
            "https://upload.wikimedia.org/wikipedia/commons/a/aa/example.ogv",
            tmp_path / "media",
            session=_ThrottledSession(),
        )

    assert "429" in str(excinfo.value)


def test_a_skipped_download_leaves_no_partial_file(tmp_path):
    with pytest.raises(SkipItem):
        download_to(
            "https://upload.wikimedia.org/wikipedia/commons/a/aa/example.ogv",
            tmp_path / "media",
            session=_ThrottledSession(),
        )

    assert list(tmp_path.iterdir()) == []
