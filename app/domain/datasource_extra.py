"""数据源 `extra`(落 datasources.capability_profile)的连接参数校验(设计稿 §3.4)。

- 所有类型:禁止形似口令 / 密钥的键。口令只能经 password 字段进 SecretStore(R2),
  不能借 extra 明文落库。
- oceanbase_oracle:只收 OB 连接所需的键;tenant 必填,经 OBProxy(缺省)时 cluster 必填。
  username 里已含 `@` 视为拼好的完整登录名(`user@tenant#cluster`),此时 tenant / cluster 可省。

其余类型的未知键暂不拒绝:存量数据源已在用若干键(dsn / service_name / charset / ssl /
connect_timeout 等),一刀切会让老数据源无法保存;按类型逐步收紧。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.domain.datasource import DbType


class DatasourceExtraError(ValueError):
    """extra 不合法。消息只含键名,不回显值。"""


_SECRET_KEY_RE = re.compile(r"(?i)(passw|pwd|secret|token|credential|private[_-]?key)")

_OB_ORACLE_KEYS = frozenset(
    {"tenant", "cluster", "access_mode", "charset", "connect_timeout", "read_timeout"}
)
_OB_ACCESS_MODES = frozenset({"proxy", "direct"})


def validate_datasource_extra(db_type: DbType, username: str, extra: Mapping[str, Any]) -> None:
    for key in extra:
        if _SECRET_KEY_RE.search(str(key)):
            raise DatasourceExtraError(
                f"extra key '{key}' looks like a secret; put credentials in the password field"
            )
    if db_type is DbType.OCEANBASE_ORACLE:
        _validate_oceanbase(username, extra, allowed=_OB_ORACLE_KEYS)


def _validate_oceanbase(
    username: str, extra: Mapping[str, Any], *, allowed: frozenset[str]
) -> None:
    unknown = sorted(str(key) for key in extra if key not in allowed)
    if unknown:
        raise DatasourceExtraError(f"unsupported OceanBase extra keys: {', '.join(unknown)}")
    access_mode = str(extra.get("access_mode") or "proxy").lower()
    if access_mode not in _OB_ACCESS_MODES:
        raise DatasourceExtraError("access_mode must be 'proxy' or 'direct'")
    if "@" in username:
        return  # 已是完整登录名
    if not str(extra.get("tenant") or "").strip():
        raise DatasourceExtraError("OceanBase tenant is required")
    if access_mode == "proxy" and not str(extra.get("cluster") or "").strip():
        raise DatasourceExtraError("OceanBase cluster is required when connecting through OBProxy")


__all__ = ["DatasourceExtraError", "validate_datasource_extra"]
