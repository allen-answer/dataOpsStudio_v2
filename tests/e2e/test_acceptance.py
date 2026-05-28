"""§5 验收 e2e —— 2.0.0 骨架完成的唯一目标。

跑通这三条 = 2.0.0 done。Step 2(Codex 实现 T1-T8)+ Step 3(拼装)
完成后,删除 @pytest.mark.xfail 即应转绿。

当前全部 xfail(strict=True),CI 看到 unexpected pass 会失败 ——
确保我们意识到 e2e 真的过了才能升级标记。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.xfail(
    strict=True, reason="2.0.0 骨架完成才转绿(Step 2 Codex T1-T7 完成 + Step 3 拼装)"
)
def test_acceptance_portable_flow() -> None:
    """
    portable 解压 → launcher 启动(PG+API+worker)→ 登录
      → 加 MySQL 数据源(密码经 SecretStore 加密)
      → 在 SQL 控制台跑一条 SELECT(经 worker + spool)
      → 前端分页看到结果
    """
    raise NotImplementedError(
        "Step 2 / Step 3 完成后实现:launcher / API / worker / MySQL adapter 全部到位"
    )


@pytest.mark.xfail(strict=True, reason="docker compose 流程同 portable(契约 §5 验收)")
def test_acceptance_docker_compose_flow() -> None:
    """docker compose(api+worker+pg)跑通同样流程。"""
    raise NotImplementedError("Step 2 / Step 3 完成后实现")


@pytest.mark.xfail(strict=True, reason="migrate_from_v1.py 完成才转绿(Codex T8)")
def test_acceptance_migrate_from_v1() -> None:
    """1.x 数据 migrate 进 2.0 PG(允许个别字段失败)。"""
    raise NotImplementedError("Codex T8(tools/migrate_from_v1.py)完成后实现")
