"""C-9 通知渠道 + 服务单测(设计稿 PR2;R1/R2/R5 + 失败隔离)。

monkeypatch urllib(不真发网络):断言 webhook POST body / 企微 markdown 模板 /
超时透传 / 非 2xx→ok=False / reveal 回调被调取密钥 / 明文 url 不出现在返回 / 失败被吞不抛。
"""

from __future__ import annotations

import json
import smtplib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage

import pytest
from structlog.testing import capture_logs

from app.domain.notify import NotifyResult, NotifyTarget, RunNotification
from app.domain.secret import SecretKind, SecretRef
from app.domain.workflow import WorkflowSpec
from app.services.notify.channels import (
    EmailChannel,
    WebhookChannel,
    WecomChannel,
    _classify_error,
    default_channels,
)
from app.services.notify.service import WorkflowNotifyService

# ★ 含 token 的整条 url —— 断言它绝不出现在返回 / 日志里(R5)。
SECRET_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SUPERSECRETTOKEN123"
MESSAGE_SENTINEL = "dag-message-sentinel"


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


def test_webhook_json_includes_provided_message(fake_urlopen: _FakeUrlopen) -> None:
    WebhookChannel().send(
        make_target(),
        make_payload(message=MESSAGE_SENTINEL),
        reveal=_RecordingReveal(),
    )
    assert fake_urlopen.recorded[0].body["message"] == MESSAGE_SENTINEL


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


def test_wecom_markdown_includes_provided_message(fake_urlopen: _FakeUrlopen) -> None:
    WecomChannel().send(
        make_target(channel="wecom"),
        make_payload(message=MESSAGE_SENTINEL),
        reveal=_RecordingReveal(),
    )
    content = fake_urlopen.recorded[0].body["markdown"]["content"]  # type: ignore[index]
    assert MESSAGE_SENTINEL in content


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


# ------------------------------------------------------------ email(SMTP)渠道

SMTP_PASSWORD = "smtp-secret-pw-456"  # 测试常量,断言绝不外泄(R5)


class _RecordingRevealPassword:
    """stub reveal:记录被调的 SecretRef,返回 SMTP 明文密码。"""

    def __init__(self) -> None:
        self.calls: list[SecretRef] = []

    def __call__(self, ref: SecretRef) -> str:
        self.calls.append(ref)
        return SMTP_PASSWORD


@dataclass
class _SMTPRecorder:
    host: str | None = None
    port: int | None = None
    timeout: int | None = None
    starttls_called: bool = False
    has_starttls: bool = True
    login: tuple[str, str] | None = None
    sent: list[EmailMessage] = field(default_factory=list)
    raise_on_send: BaseException | None = None


class _FakeSMTP:
    """充当 smtplib.SMTP:记录连接 / login / send;可配置发送时抛异常。"""

    def __init__(
        self, rec: _SMTPRecorder, host: str, port: int, timeout: int | None = None
    ) -> None:
        self._rec = rec
        rec.host = host
        rec.port = port
        rec.timeout = timeout

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def ehlo(self) -> None:
        return None

    def has_extn(self, name: str) -> bool:
        return name == "starttls" and self._rec.has_starttls

    def starttls(self) -> None:
        self._rec.starttls_called = True

    def login(self, user: str, password: str) -> None:
        self._rec.login = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        if self._rec.raise_on_send is not None:
            raise self._rec.raise_on_send
        self._rec.sent.append(message)


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> _SMTPRecorder:
    rec = _SMTPRecorder()

    def factory(host: str, port: int, timeout: int | None = None) -> _FakeSMTP:
        return _FakeSMTP(rec, host, port, timeout=timeout)

    monkeypatch.setattr(smtplib, "SMTP", factory)
    return rec


def make_email_target(**overrides: object) -> NotifyTarget:
    payload: dict[str, object] = {
        "id": "e1",
        "channel": "email",
        "smtp_host": "smtp.example.com",
        "smtp_port": 2525,
        "smtp_from": "ops@example.com",
        "smtp_to": ["team@example.com", "lead@example.com"],
        "smtp_user": "ops",
        "password_secret_ref": "pw-ref-1",
    }
    payload.update(overrides)
    return NotifyTarget.model_validate(payload)


def test_email_sends_message(fake_smtp: _SMTPRecorder) -> None:
    reveal = _RecordingRevealPassword()
    result = EmailChannel().send(make_email_target(), make_payload(), reveal=reveal)
    assert result.ok is True
    assert result.channel == "email"
    # 连接到配置的 host/port,超时透传
    assert fake_smtp.host == "smtp.example.com"
    assert fake_smtp.port == 2525
    # user + 明文密码经 login;密码来自 reveal 回调(R2)
    assert fake_smtp.login == ("ops", SMTP_PASSWORD)
    assert len(reveal.calls) == 1
    assert reveal.calls[0] == SecretRef(ref="pw-ref-1", kind=SecretKind.SMTP_PASSWORD)
    # 邮件头 + 正文含 workflow / status / error
    assert len(fake_smtp.sent) == 1
    message = fake_smtp.sent[0]
    assert message["From"] == "ops@example.com"
    assert message["To"] == "team@example.com, lead@example.com"
    assert "failed" in message["Subject"]
    assert "nightly" in message["Subject"]
    body = message.get_content()
    assert "Workflow failed: nightly" in body
    assert "run: job-9" in body
    assert "error: boom" in body


def test_email_body_includes_provided_message(fake_smtp: _SMTPRecorder) -> None:
    EmailChannel().send(
        make_email_target(),
        make_payload(message=MESSAGE_SENTINEL),
        reveal=_RecordingRevealPassword(),
    )
    assert MESSAGE_SENTINEL in fake_smtp.sent[0].get_content()


def test_email_timeout_passed_through(fake_smtp: _SMTPRecorder) -> None:
    EmailChannel().send(
        make_email_target(timeout_seconds=9), make_payload(), reveal=_RecordingRevealPassword()
    )
    assert fake_smtp.timeout == 9


def test_email_no_password_skips_login(fake_smtp: _SMTPRecorder) -> None:
    reveal = _RecordingRevealPassword()
    target = make_email_target(smtp_user=None, password_secret_ref=None)
    result = EmailChannel().send(target, make_payload(), reveal=reveal)
    assert result.ok is True
    assert fake_smtp.login is None  # 无 user/密码 → 匿名投递,不 login
    assert reveal.calls == []  # 无 ref → 不 reveal
    assert len(fake_smtp.sent) == 1


def test_email_failure_swallowed_no_password_leak(fake_smtp: _SMTPRecorder) -> None:
    # 发送时 SMTP 服务器返回错误码 —— 断言不抛、密码不外泄(R5)
    fake_smtp.raise_on_send = smtplib.SMTPResponseException(535, "auth failed")
    reveal = _RecordingRevealPassword()
    result = EmailChannel().send(make_email_target(), make_payload(), reveal=reveal)
    assert result.ok is False
    assert result.error == "smtp_error status=535"
    # ★ 即使密码已被 reveal,也绝不出现在返回结果的任何字符串里
    assert SMTP_PASSWORD not in str(result.model_dump())
    assert SMTP_PASSWORD not in (result.error or "")


def test_email_generic_exception_swallowed(fake_smtp: _SMTPRecorder) -> None:
    fake_smtp.raise_on_send = smtplib.SMTPException("boom")
    result = EmailChannel().send(
        make_email_target(), make_payload(), reveal=_RecordingRevealPassword()
    )
    assert result.ok is False
    assert result.error == "smtp_error SMTPException"


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


class _NodeRecordingChannel:
    channel = "webhook"

    def __init__(
        self,
        *,
        failed_ids: set[str] | None = None,
        raise_ids: set[str] | None = None,
    ) -> None:
        self.failed_ids = failed_ids or set()
        self.raise_ids = raise_ids or set()
        self.calls: list[str] = []

    def send(
        self,
        target: NotifyTarget,
        payload: RunNotification,
        *,
        reveal: object,
    ) -> NotifyResult:
        del payload, reveal
        self.calls.append(target.id)
        if target.id in self.raise_ids:
            raise RuntimeError(
                "raw-exception message=dag-message-sentinel "
                "url=https://sensitive.invalid/hook password=smtp-password ref=ref-abc"
            )
        if target.id in self.failed_ids:
            return NotifyResult(
                channel=self.channel,
                target_id=target.id,
                ok=False,
                error="http_error status=500",
            )
        return NotifyResult(channel=self.channel, target_id=target.id, ok=True)


def test_notify_node_selects_only_target_ids_in_declared_order() -> None:
    spec = _spec_with(
        make_target(id="t1", events=["failed"]),
        make_target(id="t2", events=["failed"]),
        make_target(id="t3", events=["failed"]),
    )
    channel = _NodeRecordingChannel()

    results = WorkflowNotifyService(
        _RecordingReveal(),
        channels={"webhook": channel},
    ).notify_node(spec, ["t3", "t1"], make_payload(status="running"))

    assert channel.calls == ["t3", "t1"]
    assert [result.target_id for result in results] == ["t3", "t1"]


def test_notify_node_skips_disabled_but_ignores_terminal_events() -> None:
    spec = _spec_with(
        make_target(id="disabled", enabled=False, events=["all"]),
        make_target(id="event-mismatch", events=["failed"]),
    )
    channel = _NodeRecordingChannel()

    results = WorkflowNotifyService(
        _RecordingReveal(),
        channels={"webhook": channel},
    ).notify_node(
        spec,
        ["disabled", "event-mismatch"],
        make_payload(status="running"),
    )

    assert channel.calls == ["event-mismatch"]
    assert [result.target_id for result in results] == ["event-mismatch"]


def test_notify_node_attempts_later_targets_after_failure() -> None:
    spec = _spec_with(
        make_target(id="first", events=["all"]),
        make_target(id="second", events=["all"]),
    )
    channel = _NodeRecordingChannel(failed_ids={"first"})

    results = WorkflowNotifyService(
        _RecordingReveal(),
        channels={"webhook": channel},
    ).notify_node(spec, ["first", "second"], make_payload(status="running"))

    assert channel.calls == ["first", "second"]
    assert [result.ok for result in results] == [False, True]


def test_notify_node_unknown_target_and_channel_are_safe_failures() -> None:
    spec = _spec_with(make_target(id="known"))

    results = WorkflowNotifyService(
        _RecordingReveal(),
        channels={},
    ).notify_node(spec, ["missing", "known"], make_payload(status="running"))

    assert [(result.target_id, result.error) for result in results] == [
        ("missing", "unknown_target"),
        ("known", "unknown_channel"),
    ]


def test_notify_node_logs_and_results_exclude_sensitive_failure_details() -> None:
    spec = _spec_with(make_target(id="bad"))
    channel = _NodeRecordingChannel(raise_ids={"bad"})
    with capture_logs() as logs:
        results = WorkflowNotifyService(
            _RecordingReveal(),
            channels={"webhook": channel},
        ).notify_node(
            spec,
            ["bad"],
            make_payload(status="running", message=MESSAGE_SENTINEL),
        )

    rendered = repr([result.model_dump() for result in results]) + repr(logs)
    assert results[0].error == "channel_error"
    for sensitive in (
        MESSAGE_SENTINEL,
        "sensitive.invalid",
        "smtp-password",
        "ref-abc",
        "raw-exception",
    ):
        assert sensitive not in rendered


def test_notify_node_normalizes_sensitive_channel_failure_result() -> None:
    class _RawFailureChannel:
        channel = "webhook"

        def send(
            self,
            target: NotifyTarget,
            payload: RunNotification,
            *,
            reveal: object,
        ) -> NotifyResult:
            del payload, reveal
            return NotifyResult(
                channel="https://sensitive.invalid/hook",
                target_id="ref-abc",
                ok=False,
                error=(
                    "response-body raw-exception dag-message-sentinel "
                    "https://sensitive.invalid/hook smtp-password ref-abc"
                ),
            )

    spec = _spec_with(make_target(id="bad"))
    results = WorkflowNotifyService(
        _RecordingReveal(),
        channels={"webhook": _RawFailureChannel()},
    ).notify_node(
        spec,
        ["bad"],
        make_payload(status="running", message=MESSAGE_SENTINEL),
    )

    assert results == [
        NotifyResult(
            channel="webhook",
            target_id="bad",
            ok=False,
            error="delivery_failed",
        )
    ]
    rendered = repr([result.model_dump() for result in results])
    for sensitive in (
        MESSAGE_SENTINEL,
        "sensitive.invalid",
        "smtp-password",
        "ref-abc",
        "response-body",
        "raw-exception",
    ):
        assert sensitive not in rendered


def test_default_channels_registry() -> None:
    channels = default_channels()
    assert set(channels) == {"webhook", "wecom", "email"}
