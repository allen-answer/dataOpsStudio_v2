"""L-4 血缘语义 domain 层:刷新方式判定 + 表角色识别 + 目标表整合 + 观察/风险。

纯函数,无 IO / 无 DB(R1:domain 层禁 DB 驱动)。移植自 1.x
``_common.normalize_table_name`` / ``roles.py`` 的分类逻辑,契约见任务书 L-4。

入口:``build_semantic_view(reports)`` 聚合批量(或单脚本)LineageReport,产出
挂载于 report 的 :class:`SemanticView`。
"""

from __future__ import annotations

import re
from collections import defaultdict

from app.domain.lineage.models import (
    LineageReport,
    RefreshMode,
    SemanticRisk,
    SemanticView,
    TableRole,
    TableRoleKind,
    TargetIntegration,
    TargetSummary,
)

# ---------------------------------------------------------------------------
# 归一 helper(移植 1.x _common.normalize_table_name)
# ---------------------------------------------------------------------------

# strip 字符集:双引号 / 反引号 / 方括号(各方言的标识符引用符)。
_STRIP_QUOTES = '"`[]'


def _normalize(name: str) -> str:
    """归一表名:去首尾空白、剥标识符引用符、转小写。"""
    return name.strip().strip(_STRIP_QUOTES).lower()


# ---------------------------------------------------------------------------
# 1. classify_refresh_mode
# ---------------------------------------------------------------------------

# insert 计数同时纳入 create(CREATE TABLE AS = 建表即写入)。
_INSERT_OPS = ("insert", "create")


# 跨脚本聚合时按严重度取最强刷新模式(全量重刷 > 分区重刷 > merge > update > mixed > append)。
_REFRESH_SEVERITY: dict[str, int] = {
    RefreshMode.TRUNCATE_INSERT.value: 7,
    RefreshMode.DELETE_INSERT.value: 6,
    RefreshMode.DELETE_INSERT_PARTIAL.value: 5,
    RefreshMode.MERGE.value: 4,
    RefreshMode.UPDATE.value: 3,
    RefreshMode.MIXED.value: 2,
    RefreshMode.APPEND.value: 1,
}


def classify_refresh_mode(ops: list[TargetSummary]) -> str | None:
    """判定**单个作用域(单 report/单脚本)**内一张表所有写操作的刷新方式。

    ``ops`` 必须来自同一 report —— before_insert 用 statement_index 交叉比较,
    只在同一脚本内有序有意义。跨脚本聚合请走 build_target_integration
    (它对每个 report 分别 classify 再按严重度取最强),**不要把多 report 的
    ops 混池传进来**,否则各自从 0 起的 statement_index 串号会误判 before_insert。

    优先级短路,返回第一个命中的 ``RefreshMode.*.value``;无写操作返回 ``None``。
    """
    if not ops:
        return None

    insert_count = sum(1 for o in ops if o.operation in _INSERT_OPS)
    update_count = sum(1 for o in ops if o.operation == "update")
    merge_count = sum(1 for o in ops if o.operation == "merge")

    insert_indices = [o.statement_index for o in ops if o.operation in _INSERT_OPS]
    delete_indices = [o.statement_index for o in ops if o.operation == "delete"]
    truncate_indices = [o.statement_index for o in ops if o.operation == "truncate"]

    delete_before_insert = any(d < i for d in delete_indices for i in insert_indices)
    truncate_before_insert = any(t < i for t in truncate_indices for i in insert_indices)
    has_full_delete = any(o.operation == "delete" and o.has_where is False for o in ops)

    if truncate_before_insert:
        return RefreshMode.TRUNCATE_INSERT.value
    if delete_before_insert and has_full_delete:
        return RefreshMode.DELETE_INSERT.value
    if delete_before_insert:
        return RefreshMode.DELETE_INSERT_PARTIAL.value
    if merge_count > 0 and insert_count == 0 and update_count == 0:
        return RefreshMode.MERGE.value
    if update_count > 0 and insert_count == 0 and merge_count == 0:
        return RefreshMode.UPDATE.value
    if insert_count > 0 and update_count == 0 and merge_count == 0:
        return RefreshMode.APPEND.value
    if insert_count > 0 or update_count > 0 or merge_count > 0:
        return RefreshMode.MIXED.value
    return None


# ---------------------------------------------------------------------------
# 2. identify_table_roles(移植 1.x roles.py)
# ---------------------------------------------------------------------------

# Oracle @dblink 后缀:scott.emp@remote_db → 命中 @remote_db。
_DBLINK_RE = re.compile(r"@[\w$#.]+$")

# schema 段整段精确匹配(小写后查表)→ 角色。
_SCHEMA_EXACT: dict[str, str] = {
    "dim": TableRoleKind.DIMENSION.value,
    "dimension": TableRoleKind.DIMENSION.value,
    "ref": TableRoleKind.REFERENCE.value,
    "reference": TableRoleKind.REFERENCE.value,
    "lookup": TableRoleKind.REFERENCE.value,
    "dict": TableRoleKind.REFERENCE.value,
    "code": TableRoleKind.REFERENCE.value,
    "codes": TableRoleKind.REFERENCE.value,
    "config": TableRoleKind.CONFIG.value,
    "conf": TableRoleKind.CONFIG.value,
    "cfg": TableRoleKind.CONFIG.value,
    "settings": TableRoleKind.CONFIG.value,
    "filter": TableRoleKind.FILTER.value,
    "filters": TableRoleKind.FILTER.value,
    "exclude": TableRoleKind.FILTER.value,
    "exclusion": TableRoleKind.FILTER.value,
    "blacklist": TableRoleKind.FILTER.value,
}

# basename 词边界正则(顺序即检测顺序;IGNORECASE)。
_BASENAME_PATTERNS: dict[str, re.Pattern[str]] = {
    TableRoleKind.CONFIG.value: re.compile(
        r"(?:^|_)(?:config|conf|cfg|settings?|params?|parameters?)(?:_|$)", re.IGNORECASE
    ),
    TableRoleKind.REFERENCE.value: re.compile(
        r"(?:^|_)(?:ref|reference|lookup|dict|codes?|enum|types?)(?:_|$)", re.IGNORECASE
    ),
    TableRoleKind.DIMENSION.value: re.compile(r"(?:^|_)(?:dim|dimensions?)(?:_|$)", re.IGNORECASE),
    TableRoleKind.FILTER.value: re.compile(
        r"(?:^|_)(?:exclude|exclusion|excl|blacklist|blocklist|exceptions?|filters?)(?:_|$)",
        re.IGNORECASE,
    ),
}

_NAMED_ROLES = frozenset(
    {
        TableRoleKind.CONFIG.value,
        TableRoleKind.REFERENCE.value,
        TableRoleKind.DIMENSION.value,
        TableRoleKind.FILTER.value,
    }
)

# primary_role 优先级(取 roles 中第一个命中)。
_PRIMARY_ORDER = [
    TableRoleKind.REMOTE_DBLINK.value,
    TableRoleKind.INTERMEDIATE.value,
    TableRoleKind.TARGET.value,
    TableRoleKind.CONFIG.value,
    TableRoleKind.REFERENCE.value,
    TableRoleKind.DIMENSION.value,
    TableRoleKind.FILTER.value,
    TableRoleKind.SOURCE_FACT.value,
]


def _pick_primary(roles: list[str], default: str = TableRoleKind.SOURCE_FACT.value) -> str:
    """按 _PRIMARY_ORDER 取第一个命中的角色;全空回退 default。"""
    return next((r for r in _PRIMARY_ORDER if r in roles), default)


def _named_roles_for(display: str) -> list[str]:
    """schema 段精确 + basename 词边界 → 命名角色列表(去重保序)。"""
    base = _DBLINK_RE.sub("", display)  # 先剥 @dblink 后缀
    parts = base.split(".")
    basename = parts[-1].strip().strip(_STRIP_QUOTES)
    schema = parts[-2].strip().strip(_STRIP_QUOTES) if len(parts) >= 2 else None

    named: list[str] = []
    if schema:
        role = _SCHEMA_EXACT.get(schema.lower())
        if role is not None:
            named.append(role)
    for role, pattern in _BASENAME_PATTERNS.items():
        if pattern.search(basename) and role not in named:
            named.append(role)
    return named


def _classify_table(display: str, in_write: bool, in_read: bool) -> TableRole:
    roles: list[str] = []

    # 结构角色(互斥):写+读=intermediate;仅写=target;仅读留待兜底。
    if in_write and in_read:
        roles.append(TableRoleKind.INTERMEDIATE.value)
    elif in_write:
        roles.append(TableRoleKind.TARGET.value)

    # remote_dblink:display 名匹配 @dblink 后缀。
    if _DBLINK_RE.search(display):
        roles.append(TableRoleKind.REMOTE_DBLINK.value)

    # 命名角色(schema 段 + basename 词边界)。
    for role in _named_roles_for(display):
        if role not in roles:
            roles.append(role)

    # 兜底:未被任何角色分类(典型:纯读且无命名)→ source_fact。
    if not roles:
        roles.append(TableRoleKind.SOURCE_FACT.value)

    return TableRole(table=display, roles=roles, primary_role=_pick_primary(roles))


def identify_table_roles(report: LineageReport) -> list[TableRole]:
    """从单个 report 识别所有出现过的表的角色,按归一名排序输出。"""
    write_display: dict[str, str] = {}
    read_display: dict[str, str] = {}

    for ts in report.target_summary:
        write_display.setdefault(_normalize(ts.table), ts.table)

    for edge in report.graph_edges:
        src = edge.get("source_table")
        if src:
            read_display.setdefault(_normalize(src), src)
    for mapping in report.insert_mappings:
        src = mapping.get("source_table")
        if src:
            read_display.setdefault(_normalize(src), src)
    for table in report.tables:
        if table.get("role") == "source":
            name = table.get("name")
            if name:
                read_display.setdefault(_normalize(name), name)

    all_norms = set(write_display) | set(read_display)
    result: list[TableRole] = []
    for norm in sorted(all_norms):
        display = write_display.get(norm) or read_display[norm]
        result.append(
            _classify_table(display, in_write=norm in write_display, in_read=norm in read_display)
        )
    return result


# ---------------------------------------------------------------------------
# 3. build_target_integration(跨脚本聚合)
# ---------------------------------------------------------------------------

_COUNT_KEYS = ("insert", "update", "merge", "delete", "truncate")


def build_target_integration(
    reports: list[tuple[str, LineageReport]],
) -> list[TargetIntegration]:
    """跨脚本按归一目标表名聚合:刷新方式 / 计数 / 源表 / 源文件 / 角色。"""
    # 先在所有 report 上跑角色识别,按归一名合并 roles。
    roles_by_norm: dict[str, list[str]] = defaultdict(list)
    for _file, report in reports:
        for role_entry in identify_table_roles(report):
            norm = _normalize(role_entry.table)
            for role in role_entry.roles:
                if role not in roles_by_norm[norm]:
                    roles_by_norm[norm].append(role)

    display: dict[str, str] = {}
    writes: dict[str, list[tuple[int, TargetSummary]]] = defaultdict(list)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {k: 0 for k in _COUNT_KEYS})
    source_tables: dict[str, list[str]] = defaultdict(list)
    source_files: dict[str, list[str]] = defaultdict(list)
    targets: set[str] = set()

    for file_index, (source_file, report) in enumerate(reports):
        for ts in report.target_summary:
            norm = _normalize(ts.table)
            targets.add(norm)
            display.setdefault(norm, ts.table)
            writes[norm].append((file_index, ts))
            key = "insert" if ts.operation in _INSERT_OPS else ts.operation
            if key in counts[norm]:
                counts[norm][key] += 1
            if source_file and source_file not in source_files[norm]:
                source_files[norm].append(source_file)
        for edge in report.graph_edges:
            tgt = edge.get("target_table")
            src = edge.get("source_table")
            if tgt and src:
                norm_tgt = _normalize(tgt)
                norm_src = _normalize(src)
                if norm_src not in source_tables[norm_tgt]:
                    source_tables[norm_tgt].append(norm_src)

    result: list[TargetIntegration] = []
    for norm in sorted(targets):
        # ★ 刷新模式必须**逐 report** 判定(each report = 一个作用域),再按严重度
        # 取最强 —— 不能把多文件 ops 混池,否则各自从 0 起的 statement_index 串号
        # 会把「文件 A 的 INSERT + 文件 B 的 DELETE」误判成全量重刷。
        per_file_ops: dict[int, list[TargetSummary]] = defaultdict(list)
        for file_index, ts in writes[norm]:
            per_file_ops[file_index].append(ts)
        modes: list[str] = []
        for file_ops in per_file_ops.values():
            mode = classify_refresh_mode(sorted(file_ops, key=lambda t: t.statement_index))
            if mode is not None:
                modes.append(mode)
        refresh_mode = max(modes, key=lambda m: _REFRESH_SEVERITY[m]) if modes else None
        roles = list(roles_by_norm.get(norm, []))
        result.append(
            TargetIntegration(
                table=display[norm],
                primary_role=_pick_primary(roles, default=TableRoleKind.TARGET.value),
                roles=roles,
                refresh_mode=refresh_mode,
                counts=dict(counts[norm]),
                source_tables=source_tables.get(norm, []),
                source_files=source_files.get(norm, []),
                titles=[],
            )
        )
    return result


# ---------------------------------------------------------------------------
# 4. build_observations
# ---------------------------------------------------------------------------

_FULL_RELOAD_MODES = (RefreshMode.TRUNCATE_INSERT.value, RefreshMode.DELETE_INSERT.value)


def build_observations(view: SemanticView) -> list[str]:
    """中文观察句,顺序固定,仅在计数>0 时产出。"""
    observations: list[str] = []

    target_count = len(view.targets)
    if target_count:
        observations.append(f"脚本共写入 {target_count} 张目标表")

    full_reload = sum(1 for t in view.targets if t.refresh_mode in _FULL_RELOAD_MODES)
    if full_reload:
        observations.append(f"其中 {full_reload} 张走全量重刷(truncate_insert / delete_insert)")

    merge = sum(1 for t in view.targets if t.refresh_mode == RefreshMode.MERGE.value)
    if merge:
        observations.append(f"{merge} 张走 MERGE 合并写入")

    intermediate = sum(
        1 for t in view.targets if t.primary_role == TableRoleKind.INTERMEDIATE.value
    )
    if intermediate:
        observations.append(f"{intermediate} 张为中转表(既写又读)")

    dblink = sum(1 for tr in view.table_roles if TableRoleKind.REMOTE_DBLINK.value in tr.roles)
    if dblink:
        observations.append(f"{dblink} 张通过 DB Link 引用远端")

    return observations


# ---------------------------------------------------------------------------
# 5. build_risks
# ---------------------------------------------------------------------------


def build_risks(reports: list[tuple[str, LineageReport]], view: SemanticView) -> list[SemanticRisk]:
    """汇总解析错误 / 低置信动态 SQL / 全量重刷无护栏三类风险。"""
    risks: list[SemanticRisk] = []

    for _file, report in reports:
        for err in report.parse_errors:
            message = err.get("message") or "解析失败"
            risks.append(SemanticRisk(level="high", type="parse_error", message=message))
        for segment in report.dynamic_sql_segments:
            confidence = segment.get("confidence")
            # 置信越低风险越高:low→medium、medium→low;high/unresolved 跳过。
            if confidence == "low":
                level = "medium"
            elif confidence == "medium":
                level = "low"
            else:
                continue
            risks.append(
                SemanticRisk(
                    level=level,
                    type="dynamic_sql_low_confidence",
                    message=f"动态 SQL(置信 {confidence}),可能漏识别血缘",
                )
            )

    for target in view.targets:
        if target.refresh_mode in _FULL_RELOAD_MODES:
            risks.append(
                SemanticRisk(
                    level="medium",
                    type="full_reload",
                    message=(
                        f"目标表 {target.table} 全量重刷({target.refresh_mode}),"
                        "重跑前请确认有备份或写入幂等"
                    ),
                )
            )

    return risks


# ---------------------------------------------------------------------------
# 6. build_semantic_view(总入口)
# ---------------------------------------------------------------------------


def _merge_table_roles(reports: list[tuple[str, LineageReport]]) -> list[TableRole]:
    """合并各 report 的角色识别结果:按归一名去重合并 roles,primary 重选。"""
    display: dict[str, str] = {}
    merged: dict[str, list[str]] = defaultdict(list)
    for _file, report in reports:
        for role_entry in identify_table_roles(report):
            norm = _normalize(role_entry.table)
            display.setdefault(norm, role_entry.table)
            for role in role_entry.roles:
                if role not in merged[norm]:
                    merged[norm].append(role)

    return [
        TableRole(table=display[norm], roles=merged[norm], primary_role=_pick_primary(merged[norm]))
        for norm in sorted(merged)
    ]


def build_semantic_view(reports: list[tuple[str, LineageReport]]) -> SemanticView:
    """总入口:整合目标表 + 合并角色,再算 observations / risks 填入。"""
    view = SemanticView(
        targets=build_target_integration(reports),
        table_roles=_merge_table_roles(reports),
    )
    view.observations = build_observations(view)
    view.risks = build_risks(reports, view)
    return view
