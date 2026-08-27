"""结构化日志(structlog)+ ★ 强制脱敏 processor。

R5 红线(契约 §4 / 设计稿 §9.2.2):日志禁止出现敏感值。
不能靠开发者自觉,必须做成 structlog 全局 processor 强制拦截:

1. 按 key 名拦截 SENSITIVE_KEY_FRAGMENTS → REDACTED
2. SQL 不记原文,只记 sql_hash + sql_len(SQL 原文是 L3,见 ai.EgressLevel)
3. 样本数据(rows/sample)永不进日志,只记 rows_omitted(L4/L5)
4. 字符串值正则兜底:身份证 / 手机号命中即打码

★ 这是辛苦做了 SecretStore 不落明文密码后的"最后一道墙"——若排障一行
`log.info("connect", password=pwd)` 把密码打进日志,前功尽弃。

已知限制(留给后续迭代,Codex 可扩):
- Pydantic BaseModel 嵌套时不递归(序列化由 JSONRenderer 完成,
  到那一步处理器已无机会)。临时方案:开发者不要直接把 BaseModel
  传入 log;先 .model_dump() 再过 _redact_value
- 错误消息中嵌入的明文密码("password='hunter2' wrong"风格)正则
  不拦截,只拦截 ID/手机号。Defense-in-depth:别把 credential 写进
  error message。
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

REDACTED = "***REDACTED***"


# 任意 key 名含以下片段(忽略大小写),其 value 一律 REDACTED。
# 注意覆盖中文常见命名 / 大小写变体 / 复合词(如 "datasource_password" 含 "password")。
SENSITIVE_KEY_FRAGMENTS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "mfa",
        "totp",
        "authorization",
        "auth_header",
        "bearer",
        "cookie",
        "session_id",
        "ciphertext",
        "encrypted",
        "private_key",
        "credential",
    }
)


# 正则兜底(设计稿 §9.2.2 第 4 条)。
# 身份证:17 位数字 + 末位 数字/X/x(18 位)。
# 大陆手机:1[3-9] 开头 11 位数字。
_ID_CARD_PATTERN = re.compile(r"\b\d{17}[\dXx]\b")
_PHONE_PATTERN = re.compile(r"\b1[3-9]\d{9}\b")


def _is_sensitive_key(key: str) -> bool:
    """key 名含敏感片段 → 命中(忽略大小写)。"""
    lower = key.lower()
    return any(frag in lower for frag in SENSITIVE_KEY_FRAGMENTS)


def _redact_value(value: Any) -> Any:
    """正则兜底 + 嵌套结构递归。

    - str:身份证/手机号打码
    - dict:递归;子 key 命中敏感时整段 REDACTED
    - list:逐项递归
    - 其它:原样返回
    """
    if isinstance(value, str):
        value = _ID_CARD_PATTERN.sub(REDACTED, value)
        value = _PHONE_PATTERN.sub(REDACTED, value)
        return value
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _is_sensitive_key(k):
                out[k] = REDACTED
            else:
                out[k] = _redact_value(v)
        return out
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def redact_processor(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor —— 强制脱敏(R5 红线,不可关闭)。

    顺序:
    1. SQL 原文 → sql_hash + sql_len
    2. rows / sample → 删除 + rows_omitted: True
    3. event_dict 顶层:key 名敏感 → REDACTED;值递归 _redact_value
    """
    # 1. SQL 不记原文(L3)
    if "sql" in event_dict:
        sql_raw = event_dict.pop("sql")
        if isinstance(sql_raw, str):
            digest = hashlib.sha256(sql_raw.encode("utf-8")).hexdigest()
            event_dict["sql_hash"] = f"sha256:{digest[:16]}"
            event_dict["sql_len"] = len(sql_raw)

    # 2. rows / sample 永不进日志(L4/L5)
    rows_present = "rows" in event_dict or "sample" in event_dict
    event_dict.pop("rows", None)
    event_dict.pop("sample", None)
    if rows_present:
        event_dict["rows_omitted"] = True

    # 3. 顶层 key 拦截 + 4. 值正则兜底
    for k in list(event_dict.keys()):
        if isinstance(k, str) and _is_sensitive_key(k):
            event_dict[k] = REDACTED
        else:
            event_dict[k] = _redact_value(event_dict[k])

    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """配置 structlog —— ★ redact_processor 强制启用,**不可移除**。

    输出 JSON 到 stdout。运行日志关联字段(request_id / job_id / user_id 等)
    通过 contextvars 注入(设计稿 §9.2.1)。

    形态差异(设计稿 §9.2.4):
    - portable:文件 logs/{app,worker,pg}/,本函数 stdout → 由 launcher 重定向
    - docker / hosted:stdout → docker logs / 客户日志系统
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        # exc_info 必须在脱敏之前渲染成字符串:否则 JSON 里只剩一个裸
        # "exc_info": true,traceback 彻底丢失(现场排 TypeError 时只能
        # 在另一个进程里重跑才拿到堆栈)。放在 redact 之前 = 堆栈文本同样
        # 过 R5 脱敏,不会把 SQL / 行数据漏出去。
        structlog.processors.format_exc_info,
        redact_processor,  # ★ R5,不可移除
        structlog.processors.JSONRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
