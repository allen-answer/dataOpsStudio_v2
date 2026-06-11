"""ApiError 与 contextmanager 的回归测试。

背景(2026-06-11 真机实锤):ApiError 曾是 frozen dataclass,在
@contextmanager(sqlalchemy engine.begin() 即此实现)内 raise 时,
contextlib 的 __exit__ 以 Python 赋值设置 exc.__traceback__,frozen 的
__setattr__ 抛 FrozenInstanceError —— 所有"事务内 raise ApiError"的
4xx 全部变 500。本测试钉死该行为不回归。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from app.api.errors import ApiError


@contextmanager
def _passthrough_cm() -> Iterator[None]:
    yield


def test_api_error_survives_contextmanager_reraise() -> None:
    with pytest.raises(ApiError) as exc_info:
        with _passthrough_cm():
            raise ApiError(401, "invalid_password", "Invalid current password")
    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "invalid_password"


def test_api_error_traceback_assignable() -> None:
    err = ApiError(404, "not_found", "x")
    err.__traceback__ = None  # frozen dataclass 会在这里抛 FrozenInstanceError
