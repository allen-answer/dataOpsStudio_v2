"""C-9 通知渠道 + 服务单测(设计稿 PR2;R1/R2/R5 + 失败隔离)。

monkeypatch urllib(不真发网络):断言 webhook POST body / 企微 markdown 模板 /
超时透传 / 非 2xx→ok=False / reveal 回调被调取密钥 / 明文 url 不出现在返回 / 失败被吞不抛。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import pytest

from app.domain.notify import NotifyResult, NotifyTarget, RunNotification
from app.domain.secret import SecretKind, SecretRef
from app.domain.workflow import WorkflowSpec
from app.services.notify.channels import (
    WebhookChannel,
    WecomChannel,
    _classify_error,
    default_channels,
)
from app.services.notify.service import WorkflowNotifyService

# ★ 含 token 的整条 url —— 断言它绝不出现在返回 / 日志里(R5)。
SECRET_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SUPERSECRETTOKEN123"


def make_target(**overrides: object) -> NotifyTarget:
    payload: dict[str, object] = {"id": "t1", "channel": "webhook", "url_secret_ref": "ref-abc"}
    payload.update(overrides)
    return NotifyTarget.model_validate(payload)


def make_payload(**overrides: object) -> RunNotification:
    payload: dict[str, object] = {
        "workflow_id": "w1",
        "workflow_name": "nightly",
        "run_job_id": "job-9",
        "status": "failed",
        "error": "boom",
    }
    payload.update(overrides)
    return RunNotification.model_validate(payload)


# ------------------------------------------------------------ fake urllib


@dataclass
class _RecordedRequest:
    url: str
    body: dict[str, object]
    timeout: int | None


@dataclass
class _FakeUrlopen:
    """记录请求;可配置抛异常模拟非 2xx / 网络错误。"""

    recorded: list[_RecordedRequest] = field(default_factory=list)
    raise_exc: BaseException | None = None

    def __call__(self, request: urllib.request.Request, timeout: int | None = None) -> _FakeUrlopen:
        assert isinstance(request.data, bytes)
        self.recorded.append(
            _RecordedRequest(
                url=request.full_url,
                body=json.loads(request.data),
                timeout=timeout,
            )
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return self

    # 充当 urlopen 返回的 context manager
    def __enter__(self) -> _FakeUrlopen:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return b"ok"


class _RecordingReveal:
    """stub reveal:记录被调的 SecretRef,返回含 token 的整条 url。"""

    def __init__(self) -> None:
        self.calls: list[SecretRef] = []

    def __call__(self, ref: SecretRef) -> str:
        self.calls.append(ref)
        return SECRET_URL


@pytest.fixture
def fake_urlopen(monkeypatch: pytest.MonkeyPatch) -> _FakeUrlopen:
    fake = _FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


# ------------------------------------------------------------ webhook 渠道


def test_webhook_posts_raw_payload(fake_urlopen: _FakeUrlopen) -> None:
    reveal = _RecordingReveal()
    result = WebhookChannel().send(make_target(), make_payload(), reveal=reveal)
    assert result.ok is True
    assert result.channel == "webhook"
    # 真发到解密出的整条 url
    assert fake_urlopen.recorded[0].url == SECRET_URL
    body = fake_urlopen.recorded[0].body
    assert body["workflow_id"] == "w1"
    assert body["status"] == "failed"
    assert body["run_job_id"] == "job-9"


def test_webhook_timeout_passed_through(fake_urlopen: _FakeUrlopen) -> None:
    WebhookChannel().send(make_target(timeout_seconds=7), make_payload(), reveal=_RecordingReveal())
    assert fake_urlopen.recorded[0].timeout == 7


def test_reveal_called_with_webhook_secret_ref(fake_urlopen: _FakeUrlopen) -> None:
    reveal = _RecordingReveal()
    WebhookChannel().send(make_target(url_secret_ref="ref-xyz"), make_payload(), reveal=reveal)
    assert len(reveal.calls) == 1
    assert reveal.calls[0] == SecretRef(ref="ref-xyz", kind=SecretKind.WEBHOOK_SECRET)


# ------------------------------------------------------------ 企微 渠道


def test_wecom_markdown_template(fake_urlopen: _FakeUrlopen) -> None:
    target = make_target(channel="wecom")
    result = WecomChannel().send(target, make_payload(), reveal=_RecordingReveal())
    assert result.ok is True
    body = fake_urlopen.recorded[0].body
    assert body["msgtype"] == "markdown"
    content = body["markdown"]["content"]  # type: ignore[index]
    assert "Workflow failed: nightly" in content
    assert "run: job-9" in content
    assert "error: boom" in content


# ------------------------------------------------------------ 失败隔离(R5:不泄 url)


def test_webhook_non_2xx_returns_ok_false_no_raise(fake_urlopen: _FakeUrlopen) -> None:
    # HTTPError 消息内嵌含 token 的 url —— 断言它不外泄
    fake_urlopen.raise_exc = urllib.error.HTTPError(SECRET_URL, 500, "err", {}, None)  # type: ignore[arg-type]
    result = WebhookChannel().send(make_target(), make_payload(), reveal=_RecordingReveal())
    assert result.ok is False
    assert result.error == "http_error status=500"
    assert "SUPERSECRETTOKEN123" not in (result.error or "")


def test_webhook_network_error_swallowed(fake_urlopen: _FakeUrlopen) -> None:
    fake_urlopen.raise_exc = urllib.error.URLError(TimeoutError("timed out"))
    result = WebhookChannel().send(make_target(), make_payload(), reveal=_RecordingReveal())
    assert result.ok is False
    assert result.error == "timeout"


def test_classify_error_never_leaks_url() -> None:
    err = urllib.error.HTTPError(SECRET_URL, 403, "forbidden", {}, None)  # type: ignore[arg-type]
    classified = _classify_error(err)
    assert "SUPERSECRETTOKEN123" not in classified
    assert classified == "http_error status=403"


# ------------------------------------------------------------ WorkflowNotifyService


def _spec_with(*targets: NotifyTarget) -> WorkflowSpec:
    return WorkflowSpec.model_validate(
        {
            "nodes": [{"id": "n1", "job_kind": "sql_query", "payload": {}, "timeout_seconds": 60}],
            "notifications": [t.model_dump() for t in targets],
        }
    )


def test_service_event_filter_skips_unmatched(fake_urlopen: _FakeUrlopen) -> None:
    spec = _spec_with(make_target(events=["failed"]))
    results = WorkflowNotifyService(_RecordingReveal()).notify(spec, make_payload(), "success")
    assert results == []
    assert fake_urlopen.recorded == []


def test_service_disabled_target_skipped(fake_urlopen: _FakeUrlopen) -> None:
    spec = _spec_with(make_target(enabled=False, events=["all"]))
    results = WorkflowNotifyService(_RecordingReveal()).notify(spec, make_payload(), "failed")
    assert results == []


def test_service_dispatches_matched(fake_urlopen: _FakeUrlopen) -> None:
    spec = _spec_with(make_target(events=["failed"]))
    results = WorkflowNotifyService(_RecordingReveal()).notify(spec, make_payload(), "failed")
    assert len(results) == 1
    assert results[0].ok is True


def test_service_isolation_one_target_failure_does_not_block_others() -> None:
    """★ 一个 target 抛异常,其它 target 仍发送;service 不向上抛。"""

    class _BoomChannel:
        channel = "webhook"

        def send(
            self, target: NotifyTarget, payload: RunNotification, *, reveal: object
        ) -> NotifyResult:
            raise RuntimeError("channel misbehaved")

    class _OkChannel:
        channel = "wecom"

        def send(
            self, target: NotifyTarget, payload: RunNotification, *, reveal: object
        ) -> NotifyResult:
            return NotifyResult(channel="wecom", target_id=target.id, ok=True)

    spec = _spec_with(
        make_target(id="bad", channel="webhook", events=["all"]),
        make_target(id="good", channel="wecom", events=["all"]),
    )
    service = WorkflowNotifyService(
        _RecordingReveal(), channels={"webhook": _BoomChannel(), "wecom": _OkChannel()}
    )
    results = service.notify(spec, make_payload(), "failed")
    assert len(results) == 2
    by_id = {r.target_id: r for r in results}
    assert by_id["bad"].ok is False
    assert by_id["bad"].error == "channel_error"
    assert by_id["good"].ok is True


def test_service_unknown_channel_recorded(fake_urlopen: _FakeUrlopen) -> None:
    spec = _spec_with(make_target(events=["all"]))
    # 空注册表 → channel 未知
    service = WorkflowNotifyService(_RecordingReveal(), channels={})
    results = service.notify(spec, make_payload(), "failed")
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error == "unknown_channel"


def test_default_channels_registry() -> None:
    channels = default_channels()
    assert set(channels) == {"webhook", "wecom"}
