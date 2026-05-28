from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class LicenseMode(StrEnum):
    """License 运行态(设计稿 §8.4 + §8.5 Repair Mode)。

    invalid_signature / expired(>7d) 进入 REPAIR,**不再拒绝启动**。
    Repair Mode 允许:换 license / 备份数据 / 只读查看;
    禁止:执行 SQL / 跑任务 / AI 调用 / 业务结果导出。

    理由(设计稿 §8.5):离线金融客户现场,license 传输出错(BOM /
    截断)是常见情况,拒启动会让客户连"上传新 license"都做不到。
    """

    TRIAL = "trial"
    VALID = "valid"
    IN_GRACE = "in_grace"  # 过期 ≤7 天:只读 + 允许换 license + 备份
    REPAIR = "repair"  # invalid_signature / expired >7d / 文件损坏


class LicenseState(BaseModel):
    """License 当前状态(设计稿 §8.6 verify_license 返回值)。

    REPAIR 模式下 edition / customer / features 可空,
    UI 显示 repair_reason 引导用户上传新 license。
    """

    model_config = ConfigDict(frozen=True)

    mode: LicenseMode
    edition: str | None = None  # portable / onprem / enterprise
    customer: str | None = None
    expires_at: datetime | None = None
    features: list[str] = Field(default_factory=list)
    repair_reason: str | None = None  # invalid_signature / expired / 等
