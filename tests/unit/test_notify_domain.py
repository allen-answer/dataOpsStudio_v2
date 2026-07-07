"""C-9 通知配置模型单测(设计稿 PR1;R2/R5 域侧)。

覆盖:NotifyTarget channel 白名单、events 白名单/默认/非空、id 校验、
url_secret_ref 非空(★ 模型没有明文 url 字段)、accepts 事件匹配、
RunNotification error 截断(R5)、WorkflowSpec.notifications round-trip +
重复 target id 拒绝。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.notify import (
    ERROR_SUMMARY_MAX_LEN,
    NotifyResult,
    NotifyTarget,
    RunNotification,
)
from app.domain.workflow import WorkflowSpec


def make_target(**overrides: object) -> NotifyTarget:
    payload: dict[str, object] = {
        "id": "t1",
        "channel": "webhook",
        "url_secret_ref": "ref-abc",
    }
    payload.update(overrides)
    return NotifyTarget.model_validate(payload)


# ------------------------------------------------------------ NotifyTarget 校验


def test_defaults_events_failed_and_enabled() -> None:
    target = make_target()
    assert target.events == ["failed"]
    assert target.enabled is True
    assert target.timeout_seconds == 5


@pytest.mark.parametrize("channel", ["slack", "sms", "", "Webhook"])
def test_invalid_channel_rejected(channel: str) -> None:
    with pytest.raises(ValidationError):
        make_target(channel=channel)


@pytest.mark.parametrize("channel", ["webhook", "wecom"])
def test_supported_channels_accepted(channel: str) -> None:
    assert make_target(channel=channel).channel == channel


# ------------------------------------------------------------ email(SMTP)渠道校验


def make_email_target(**overrides: object) -> NotifyTarget:
    payload: dict[str, object] = {
        "id": "e1",
        "channel": "email",
        "smtp_host": "smtp.example.com",
        "smtp_from": "ops@example.com",
        "smtp_to": ["team@example.com"],
        "password_secret_ref": "pw-ref-1",
    }
    payload.update(overrides)
    return NotifyTarget.model_validate(payload)


def test_email_target_accepted_with_required_fields() -> None:
    target = make_email_target()
    assert target.channel == "email"
    assert target.smtp_host == "smtp.example.com"
    assert target.smtp_port == 587  # 默认提交端口
    assert target.password_secret_ref == "pw-ref-1"
    # ★ R2:模型无明文密码字段,只有 password_secret_ref
    assert "password" not in NotifyTarget.model_fields
    assert "password_secret_ref" in NotifyTarget.model_fields


def test_email_target_does_not_require_url_secret_ref() -> None:
    # email 目标不需要 url_secret_ref(webhook/wecom 才要)
    assert make_email_target().url_secret_ref is None


@pytest.mark.parametrize("missing", ["smtp_host", "smtp_from", "smtp_to"])
def test_email_target_missing_required_field_rejected(missing: str) -> None:
    overrides: dict[str, object] = {missing: [] if missing == "smtp_to" else "  "}
    with pytest.raises(ValidationError, match="invalid_email_target"):
        make_email_target(**overrides)


def test_email_password_optional_for_anonymous_relay() -> None:
    # 无密码(匿名 relay)仍是合法 email 目标
    target = make_email_target(password_secret_ref=None)
    assert target.password_secret_ref is None


def test_webhook_target_unaffected_by_email_fields() -> None:
    # webhook 目标不需要 email 字段,仍要求 url_secret_ref
    target = make_target(channel="webhook")
    assert target.smtp_host is None
    with pytest.raises(ValidationError, match="invalid_url_secret_ref"):
        make_target(channel="webhook", url_secret_ref=None)


def test_invalid_event_rejected() -> None:
    with pytest.raises(ValidationError):
        make_target(events=["bogus"])


def test_empty_events_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid_notify_events"):
        make_target(events=[])


def test_blank_id_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid_notify_target_id"):
        make_target(id="   ")


def test_blank_url_secret_ref_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid_url_secret_ref"):
        make_target(url_secret_ref="  ")


def test_no_plaintext_url_field() -> None:
    """★ R2:模型只有 url_secret_ref,没有任何明文 url 字段。"""
    assert "url" not in NotifyTarget.model_fields
    assert "url_secret_ref" in NotifyTarget.model_fields


def test_timeout_bounds() -> None:
    with pytest.raises(ValidationError):
        make_target(timeout_seconds=0)
    with pytest.raises(ValidationError):
        make_target(timeout_seconds=61)


# ------------------------------------------------------------ accepts 事件匹配


def test_accepts_exact_match() -> None:
    target = make_target(events=["failed"])
    assert target.accepts("failed") is True
    assert target.accepts("success") is False


def test_accepts_all() -> None:
    target = make_target(events=["all"])
    assert target.accepts("failed") is True
    assert target.accepts("success") is True
    assert target.accepts("cancelled") is True


# ------------------------------------------------------------ RunNotification


def test_run_notification_error_truncated() -> None:
    long_error = "x" * (ERROR_SUMMARY_MAX_LEN + 100)
    payload = RunNotification(workflow_id="w1", run_job_id="j1", status="failed", error=long_error)
    assert payload.error is not None
    assert len(payload.error) < len(long_error)
    assert payload.error.endswith("…(truncated)")


def test_run_notification_short_error_untouched() -> None:
    payload = RunNotification(workflow_id="w1", run_job_id="j1", status="failed", error="boom")
    assert payload.error == "boom"


def test_notify_result_shape() -> None:
    result = NotifyResult(
        channel="webhook", target_id="t1", ok=False, error="http_error status=500"
    )
    assert result.ok is False
    assert "url" not in NotifyResult.model_fields


# ------------------------------------------------------------ WorkflowSpec 集成


def _node() -> dict[str, object]:
    return {
        "id": "n1",
        "job_kind": "sql_query",
        "payload": {"sql": "SELECT 1"},
        "timeout_seconds": 60,
    }


def test_workflow_spec_without_notifications() -> None:
    spec = WorkflowSpec.model_validate({"nodes": [_node()]})
    assert spec.notifications == []


def test_workflow_spec_notifications_round_trip() -> None:
    raw = {
        "nodes": [_node()],
        "notifications": [
            {"id": "t1", "channel": "webhook", "url_secret_ref": "ref-1"},
            {"id": "t2", "channel": "wecom", "url_secret_ref": "ref-2", "events": ["all"]},
        ],
    }
    spec = WorkflowSpec.model_validate(raw)
    dumped = spec.model_dump(mode="json")
    again = WorkflowSpec.model_validate(dumped)
    assert again == spec
    assert [t.id for t in again.notifications] == ["t1", "t2"]
    # ★ R2:序列化后 dag_jsonb 里没有明文 url,只有 *_secret_ref。
    assert "ref-1" in str(dumped)
    assert "url" not in {k for t in dumped["notifications"] for k in t}


def test_duplicate_notify_target_id_rejected() -> None:
    raw = {
        "nodes": [_node()],
        "notifications": [
            {"id": "dup", "channel": "webhook", "url_secret_ref": "ref-1"},
            {"id": "dup", "channel": "wecom", "url_secret_ref": "ref-2"},
        ],
    }
    with pytest.raises(ValidationError, match="duplicate_notify_target_id"):
        WorkflowSpec.model_validate(raw)
