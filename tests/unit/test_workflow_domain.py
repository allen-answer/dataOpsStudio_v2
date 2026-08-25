"""Workflow 领域模型单测(设计稿 §2.8、R7、用户拍板语义)。

覆盖:R7 拒绝(forbidden)与白名单内暂不支持(unsupported)两层错误区分、
DAG 环检测(直接 + 间接)、边引用不存在节点、自环、retry 边界、
条件/失败边、intrinsic payload、输出引用、cron、when 与合法 spec 往返。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.job import ALLOWED_WORKFLOW_NODE_KINDS
from app.domain.workflow import (
    BUILTIN_VARIABLE_NAMES,
    MAX_VARIABLE_LIST_LENGTH,
    MAX_VARIABLE_VALUE_LENGTH,
    MAX_WORKFLOW_DDL_TEXT_CHARS,
    MAX_WORKFLOW_VARIABLES,
    SUPPORTED_WORKFLOW_NODE_KINDS_V1,
    CronSchedule,
    RetryPolicy,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    validate_workflow_variables,
)
from app.domain.workflow_when import builtin_when_variables


def make_node(node_id: str = "n1", **overrides: object) -> WorkflowNode:
    payload: dict[str, object] = {
        "id": node_id,
        "job_kind": "sql_query",
        "payload": {"sql": "SELECT 1"},
        "timeout_seconds": 60,
    }
    payload.update(overrides)
    return WorkflowNode.model_validate(payload)


# ---------------------------------------------------------------- 节点 kind 两层校验


@pytest.mark.parametrize("kind", ["shell", "http_request", "python_script", "ddl_exec", ""])
def test_forbidden_node_kind_rejected_as_r7(kind: str) -> None:
    """R7 白名单外的 kind → forbidden_node_kind(红线违规,非「暂不支持」)。"""
    with pytest.raises(ValidationError, match="forbidden_node_kind"):
        make_node(job_kind=kind)


@pytest.mark.parametrize(
    "kind", sorted(ALLOWED_WORKFLOW_NODE_KINDS - SUPPORTED_WORKFLOW_NODE_KINDS_V1)
)
def test_allowed_but_unsupported_kind_rejected_as_unsupported(kind: str) -> None:
    """白名单内但 2.4.x 暂不支持的 Scenario 节点 → unsupported_node_kind。"""
    with pytest.raises(ValidationError, match="unsupported_node_kind"):
        make_node(job_kind=kind)


def test_unsupported_and_forbidden_messages_are_distinct() -> None:
    with pytest.raises(ValidationError) as forbidden:
        make_node(job_kind="shell")
    with pytest.raises(ValidationError) as unsupported:
        make_node(job_kind="scenario_run_all")
    assert "forbidden_node_kind" in str(forbidden.value)
    assert "unsupported_node_kind" not in str(forbidden.value)
    assert "unsupported_node_kind" in str(unsupported.value)
    assert "forbidden_node_kind" not in str(unsupported.value)


def _valid_payload_for_kind(kind: str) -> dict[str, object]:
    if kind == "branch":
        return {}
    if kind == "sleep":
        return {"duration_seconds": 30}
    if kind == "notify":
        return {"target_ids": ["notify-1"], "message": "done"}
    return {"sql": "SELECT 1"}


@pytest.mark.parametrize("kind", sorted(SUPPORTED_WORKFLOW_NODE_KINDS_V1))
def test_all_v1_supported_kinds_accepted(kind: str) -> None:
    assert make_node(job_kind=kind, payload=_valid_payload_for_kind(kind)).job_kind == kind


def test_v1_supported_set_is_subset_of_r7_allowlist() -> None:
    assert SUPPORTED_WORKFLOW_NODE_KINDS_V1 <= ALLOWED_WORKFLOW_NODE_KINDS


def test_supported_workflow_kinds_open_intrinsics_only() -> None:
    assert SUPPORTED_WORKFLOW_NODE_KINDS_V1 == ALLOWED_WORKFLOW_NODE_KINDS - {
        "scenario_materialize",
        "scenario_run_all",
    }
    assert len(SUPPORTED_WORKFLOW_NODE_KINDS_V1) == 8


# ---------------------------------------------------------------- 节点其余字段


@pytest.mark.parametrize("value", ["abort", "continue", "branch"])
def test_on_failure_literal_values_accepted_on_node(value: str) -> None:
    assert make_node(on_failure=value).on_failure == value


def test_on_failure_out_of_literal_rejected() -> None:
    with pytest.raises(ValidationError):
        make_node(on_failure="retry_forever")


def test_node_id_blank_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid_node_id"):
        make_node(node_id="   ")


def test_timeout_seconds_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        make_node(timeout_seconds=0)


def test_when_stored_and_blank_rejected() -> None:
    node = make_node(when="prev.status == 'success'")
    assert node.when == "prev.status == 'success'"
    assert make_node().when is None
    with pytest.raises(ValidationError, match="invalid_when"):
        make_node(when="   ")
    with pytest.raises(ValidationError):
        make_node(when="x" * 513)


def test_branch_payload_must_be_empty() -> None:
    assert make_node(job_kind="branch", payload={}).payload == {}
    with pytest.raises(ValidationError, match="invalid_branch_payload"):
        make_node(job_kind="branch", payload={"expression": "true"})


@pytest.mark.parametrize("duration", [1, 30, 86_400])
def test_sleep_payload_duration_bounds(duration: int) -> None:
    node = make_node(job_kind="sleep", payload={"duration_seconds": duration})
    assert node.payload == {"duration_seconds": duration}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"duration_seconds": 0},
        {"duration_seconds": 86_401},
        {"duration_seconds": True},
        {"duration_seconds": 1, "extra": 1},
    ],
)
def test_sleep_payload_invalid_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="invalid_sleep_payload"):
        make_node(job_kind="sleep", payload=payload)


def test_notify_payload_shape_and_caps() -> None:
    node = make_node(
        job_kind="notify",
        payload={"target_ids": ["a", "b"], "message": "finished"},
    )
    assert node.payload["target_ids"] == ["a", "b"]
    payloads: tuple[dict[str, object], ...] = (
        {},
        {"target_ids": []},
        {"target_ids": ["a", "a"]},
        {"target_ids": [""]},
        {"target_ids": ["a"], "message": "x" * 513},
        {"target_ids": ["a"], "extra": True},
    )
    for payload in payloads:
        with pytest.raises(ValidationError, match="invalid_notify_payload"):
            make_node(job_kind="notify", payload=payload)


def test_lineage_analyze_node_accepts_ddl_text() -> None:
    """F11:ddl_text 在 workflow 节点上**真正生效**(worker 侧接通),不再被静默忽略。"""
    node = make_node(
        job_kind="lineage_analyze",
        payload={
            "datasource_id": "ds-1",
            "sql_text": "select 1",
            "ddl_text": "CREATE TABLE t (a INT)",
        },
    )

    assert node.payload["ddl_text"] == "CREATE TABLE t (a INT)"


def test_lineage_analyze_node_caps_inline_ddl_text() -> None:
    """workflow spec 内联存储且按计划反复执行,上限必须比交互式解析紧。"""
    with pytest.raises(ValidationError, match="invalid_lineage_analyze_payload"):
        make_node(
            job_kind="lineage_analyze",
            payload={
                "datasource_id": "ds-1",
                "sql_text": "select 1",
                "ddl_text": "x" * (MAX_WORKFLOW_DDL_TEXT_CHARS + 1),
            },
        )


def test_lineage_analyze_node_rejects_non_string_ddl_text() -> None:
    with pytest.raises(ValidationError, match="invalid_lineage_analyze_payload"):
        make_node(
            job_kind="lineage_analyze",
            payload={"datasource_id": "ds-1", "sql_text": "select 1", "ddl_text": 123},
        )


def test_lineage_analyze_node_without_ddl_text_is_still_accepted() -> None:
    node = make_node(
        job_kind="lineage_analyze",
        payload={"datasource_id": "ds-1", "sql_text": "select 1"},
    )

    assert node.payload["sql_text"] == "select 1"


# ---------------------------------------------------------------- RetryPolicy 边界


def test_retry_policy_bounds() -> None:
    policy = RetryPolicy(max_retries=5, backoff_seconds=3600)
    assert policy.max_retries == 5
    assert policy.backoff_seconds == 3600
    assert RetryPolicy(max_retries=0, backoff_seconds=0).max_retries == 0
    with pytest.raises(ValidationError):
        RetryPolicy(max_retries=6, backoff_seconds=0)
    with pytest.raises(ValidationError):
        RetryPolicy(max_retries=-1, backoff_seconds=0)
    with pytest.raises(ValidationError):
        RetryPolicy(max_retries=0, backoff_seconds=-1)
    with pytest.raises(ValidationError):
        RetryPolicy(max_retries=0, backoff_seconds=3601)


def test_node_retry_policy_defaults_to_none_meaning_inherit_global() -> None:
    assert make_node().retry_policy is None


# ---------------------------------------------------------------- CronSchedule


def test_cron_valid_five_fields() -> None:
    schedule = CronSchedule(cron="0 2 * * 1-5")
    assert schedule.enabled is True
    assert CronSchedule(cron="*/15 0,12 1-10 * *", enabled=False).enabled is False


@pytest.mark.parametrize(
    "expr",
    [
        "0 2 * *",  # 4 段
        "0 2 * * * *",  # 6 段
        "",  # 空
        "0 2 * * mon",  # 字母不在受限字符集
        "0 2 * * ?",  # quartz 问号不支持
    ],
)
def test_cron_invalid_rejected(expr: str) -> None:
    with pytest.raises(ValidationError, match="invalid_cron"):
        CronSchedule(cron=expr)


# ---------------------------------------------------------------- Spec DAG 结构校验


def make_spec(
    node_ids: list[str], edge_pairs: list[tuple[str, str]], **overrides: object
) -> WorkflowSpec:
    payload: dict[str, object] = {
        "nodes": [make_node(node_id) for node_id in node_ids],
        "edges": [WorkflowEdge(source=s, target=t) for s, t in edge_pairs],
    }
    payload.update(overrides)
    return WorkflowSpec.model_validate(payload)


def test_single_node_no_edges_is_valid() -> None:
    spec = make_spec(["only"], [])
    assert [node.id for node in spec.nodes] == ["only"]


def test_isolated_node_alongside_chain_is_valid() -> None:
    spec = make_spec(["a", "b", "lonely"], [("a", "b")])
    assert len(spec.nodes) == 3


def test_duplicate_node_id_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate_node_id"):
        make_spec(["a", "a"], [])


def test_empty_nodes_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkflowSpec(nodes=[], edges=[])


def test_node_count_upper_bound_50() -> None:
    ids = [f"n{i}" for i in range(50)]
    assert len(make_spec(ids, []).nodes) == 50
    with pytest.raises(ValidationError):
        make_spec([f"n{i}" for i in range(51)], [])


@pytest.mark.parametrize("edge", [("ghost", "a"), ("a", "ghost")])
def test_edge_referencing_unknown_node_rejected(edge: tuple[str, str]) -> None:
    with pytest.raises(ValidationError, match="unknown_edge_node"):
        make_spec(["a", "b"], [edge])


def test_self_loop_rejected() -> None:
    with pytest.raises(ValidationError, match="self_loop"):
        make_spec(["a"], [("a", "a")])


def test_workflow_edge_routing_fields_default_and_roundtrip() -> None:
    legacy = WorkflowEdge(source="a", target="b")
    assert legacy.trigger == "success"
    assert legacy.when is None
    assert legacy.is_default is False

    conditional = WorkflowEdge(
        source="a",
        target="b",
        trigger="failure",
        when="${nodes.a.error_code} == 'timeout'",
    )
    assert WorkflowEdge.model_validate(conditional.model_dump()) == conditional


def test_workflow_edge_blank_condition_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid_edge_when"):
        WorkflowEdge(source="a", target="b", when="   ")


def _reported_cycle_nodes(error: ValidationError) -> set[str]:
    """从错误信息中提取「环上节点: x -> y -> z」报告的节点集合。"""
    match = re.search(r"环上节点: ([^\[\n]+)", str(error))
    assert match is not None, f"cycle nodes missing in: {error}"
    return {part.strip() for part in match.group(1).split("->")}


def test_direct_cycle_rejected_with_cycle_nodes() -> None:
    with pytest.raises(ValidationError, match="cycle_detected") as exc:
        make_spec(["a", "b"], [("a", "b"), ("b", "a")])
    assert _reported_cycle_nodes(exc.value) == {"a", "b"}


def test_indirect_cycle_rejected_with_cycle_nodes() -> None:
    with pytest.raises(ValidationError, match="cycle_detected") as exc:
        make_spec(
            ["entry", "a", "b", "c", "tail"],
            [("entry", "a"), ("a", "b"), ("b", "c"), ("c", "a"), ("c", "tail")],
        )
    # 报告的环节点恰为 a/b/c,环外前驱 entry 与下游 tail 不在其中
    assert _reported_cycle_nodes(exc.value) == {"a", "b", "c"}


def test_diamond_dag_is_acyclic_and_valid() -> None:
    spec = make_spec(["a", "b", "c", "d"], [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")])
    assert len(spec.edges) == 4


def test_duplicate_edge_by_source_target_and_trigger_rejected() -> None:
    nodes = [make_node("a"), make_node("b")]
    edges = [
        WorkflowEdge(source="a", target="b"),
        WorkflowEdge(source="a", target="b"),
    ]
    with pytest.raises(ValidationError, match="duplicate_edge"):
        WorkflowSpec(nodes=nodes, edges=edges)


def test_ordinary_success_edge_cannot_be_conditional() -> None:
    with pytest.raises(ValidationError, match="conditional_edge_requires_branch"):
        WorkflowSpec(
            nodes=[make_node("a"), make_node("b")],
            edges=[WorkflowEdge(source="a", target="b", when="${env} == 'prod'")],
            variables={"env": "prod"},
        )


def test_branch_success_routes_require_first_match_conditions_and_default() -> None:
    nodes = [
        make_node("route", job_kind="branch", payload={}),
        make_node("prod"),
        make_node("fallback"),
    ]
    edges = [
        WorkflowEdge(source="route", target="prod", when="${env} == 'prod'"),
        WorkflowEdge(source="route", target="fallback", is_default=True),
    ]
    spec = WorkflowSpec(nodes=nodes, edges=edges, variables={"env": "prod"})
    assert [edge.target for edge in spec.edges] == ["prod", "fallback"]

    with pytest.raises(ValidationError, match="invalid_branch_routes"):
        WorkflowSpec(nodes=nodes, edges=edges[:1], variables={"env": "prod"})


def test_failure_routes_require_on_failure_branch_and_one_default() -> None:
    failing = make_node("source", on_failure="branch")
    recovery = make_node("recovery")
    valid = WorkflowSpec(
        nodes=[failing, recovery],
        edges=[
            WorkflowEdge(
                source="source",
                target="recovery",
                trigger="failure",
                is_default=True,
            )
        ],
    )
    assert valid.edges[0].trigger == "failure"

    with pytest.raises(ValidationError, match="failure_edge_requires_branch"):
        WorkflowSpec(
            nodes=[make_node("source"), recovery],
            edges=[
                WorkflowEdge(
                    source="source",
                    target="recovery",
                    trigger="failure",
                    is_default=True,
                )
            ],
        )

    with pytest.raises(ValidationError, match="missing_failure_route"):
        WorkflowSpec(nodes=[failing], edges=[])


def test_notify_node_targets_must_exist_in_workflow_notifications() -> None:
    notify_node = make_node(
        "notify",
        job_kind="notify",
        payload={"target_ids": ["ops"]},
    )
    with pytest.raises(ValidationError, match="unknown_notify_target"):
        WorkflowSpec(nodes=[notify_node], edges=[])

    spec = WorkflowSpec.model_validate(
        {
            "nodes": [notify_node],
            "edges": [],
            "notifications": [
                {
                    "id": "ops",
                    "channel": "webhook",
                    "url_secret_ref": "secret-ref",
                    "events": ["all"],
                }
            ],
        }
    )
    assert spec.nodes[0].payload["target_ids"] == ["ops"]


def test_node_output_reference_must_be_whitelisted_strict_ancestor() -> None:
    query = make_node("query")
    export = make_node(
        "export",
        job_kind="export_excel",
        payload={"source_result_set_id": "${nodes.query.result_set_id}"},
    )
    spec = WorkflowSpec(
        nodes=[query, export],
        edges=[WorkflowEdge(source="query", target="export")],
    )
    assert spec.nodes[1].payload["source_result_set_id"] == "${nodes.query.result_set_id}"

    with pytest.raises(ValidationError, match="non_upstream_node_output"):
        WorkflowSpec(
            nodes=[query, export],
            edges=[],
        )

    bad_field = export.model_copy(
        update={"payload": {"source_result_set_id": "${nodes.query.rows}"}}
    )
    with pytest.raises(ValidationError, match="forbidden_node_output_field"):
        WorkflowSpec(
            nodes=[query, bad_field],
            edges=[WorkflowEdge(source="query", target="export")],
        )


def test_edge_condition_can_reference_source_or_source_ancestor() -> None:
    root = make_node("root")
    route = make_node("route", job_kind="branch", payload={})
    left = make_node("left")
    right = make_node("right")
    spec = WorkflowSpec(
        nodes=[root, route, left, right],
        edges=[
            WorkflowEdge(source="root", target="route"),
            WorkflowEdge(
                source="route",
                target="left",
                when=("${nodes.root.loaded_rows} > 0 && ${nodes.route.status} == 'success'"),
            ),
            WorkflowEdge(source="route", target="right", is_default=True),
        ],
    )
    assert spec.edges[1].when is not None


def test_unknown_node_output_reference_rejected_at_spec_construction() -> None:
    with pytest.raises(ValidationError, match="unknown_node_output"):
        WorkflowSpec(
            nodes=[
                make_node(
                    "consumer",
                    payload={"sql": "SELECT '${nodes.ghost.status}'"},
                )
            ],
            edges=[],
        )


def test_invalid_output_reference_validation_does_not_echo_spec_payload() -> None:
    raw_expression = "nodes.query.bad-field"
    payload_secret = "sensitive-payload-value"
    consumer = make_node(
        "consumer",
        payload={
            "sql": f"SELECT '${{{raw_expression}}}'",
            "metadata": payload_secret,
        },
    )

    with pytest.raises(ValidationError) as exc_info:
        WorkflowSpec(nodes=[consumer], edges=[])

    message = str(exc_info.value)
    assert "invalid_node_output_reference" in message
    assert raw_expression not in message
    assert payload_secret not in message


# ---------------------------------------------------------------- 合法 spec 全字段往返


def test_full_spec_construction_roundtrip() -> None:
    spec = WorkflowSpec(
        nodes=[
            WorkflowNode(
                id="extract",
                job_kind="sql_query",
                payload={"sql": "SELECT * FROM t", "datasource_id": "ds-1"},
                retry_policy=RetryPolicy(max_retries=3, backoff_seconds=30),
                timeout_seconds=600,
                on_failure="abort",
                when=None,
            ),
            WorkflowNode(
                id="verify",
                job_kind="compare_run",
                payload={"task_id": "cmp-1"},
                retry_policy=None,
                timeout_seconds=1800,
                on_failure="continue",
                when="extract.status == 'success'",
            ),
        ],
        edges=[WorkflowEdge(source="extract", target="verify")],
        schedule=CronSchedule(cron="0 2 * * 1-5", enabled=True),
    )
    dumped = spec.model_dump()
    restored = WorkflowSpec.model_validate(dumped)
    assert restored == spec
    assert restored.nodes[0].retry_policy == RetryPolicy(max_retries=3, backoff_seconds=30)
    assert restored.nodes[1].retry_policy is None
    assert restored.schedule is not None
    assert restored.schedule.cron == "0 2 * * 1-5"
    # JSON 往返(API 层将以 JSON 收发 spec)
    restored_json = WorkflowSpec.model_validate_json(spec.model_dump_json())
    assert restored_json == spec


# ---------------------------------------------------------------- C-7 PR2 变量来源校验


def test_builtin_variable_names_match_when_snapshot_keys_no_drift() -> None:
    # 防漂移:冲突禁用名集合必须与 builtin_when_variables 实际产出的 key 一致
    snapshot_keys = set(builtin_when_variables(datetime(2026, 7, 7, tzinfo=UTC)))
    assert BUILTIN_VARIABLE_NAMES == snapshot_keys


def test_valid_variables_accepted_on_spec() -> None:
    spec = WorkflowSpec.model_validate(
        {
            "nodes": [make_node("n1")],
            "edges": [],
            "variables": {"biz_date": "2026-07-07", "env": "prod", "region": "ap-east-1"},
        }
    )
    assert spec.variables == {"biz_date": "2026-07-07", "env": "prod", "region": "ap-east-1"}


def test_variables_default_empty_and_roundtrip() -> None:
    spec = WorkflowSpec.model_validate({"nodes": [make_node("n1")], "edges": []})
    assert spec.variables == {}
    assert WorkflowSpec.model_validate(spec.model_dump()) == spec


def test_variable_key_colliding_with_builtin_rejected() -> None:
    for builtin in sorted(BUILTIN_VARIABLE_NAMES):
        with pytest.raises(ValidationError, match="variable_name_collides_builtin"):
            WorkflowSpec.model_validate(
                {"nodes": [make_node("n1")], "edges": [], "variables": {builtin: "x"}}
            )


@pytest.mark.parametrize("bad_key", ["1biz", "has-dash", "has.dot", "has space", "", "a b"])
def test_variable_bad_key_format_rejected(bad_key: str) -> None:
    with pytest.raises(ValidationError, match="invalid_variable_name"):
        WorkflowSpec.model_validate(
            {"nodes": [make_node("n1")], "edges": [], "variables": {bad_key: "x"}}
        )


def test_variable_count_cap_enforced() -> None:
    ok = {f"v{i}": "x" for i in range(MAX_WORKFLOW_VARIABLES)}
    assert validate_workflow_variables(ok) == ok
    too_many = {f"v{i}": "x" for i in range(MAX_WORKFLOW_VARIABLES + 1)}
    with pytest.raises(ValueError, match="too_many_variables"):
        validate_workflow_variables(too_many)


def test_variable_value_length_cap_enforced() -> None:
    long_value = "a" * (MAX_VARIABLE_VALUE_LENGTH + 1)
    with pytest.raises(ValueError, match="variable_value_too_long"):
        validate_workflow_variables({"v": long_value})


@pytest.mark.parametrize(
    "allowed",
    ["2026-07-07", "prod", "ap-east-1", "schema_a.table_b", "host:5432", "v1.2.3", ""],
)
def test_variable_safe_values_accepted(allowed: str) -> None:
    assert validate_workflow_variables({"v": allowed}) == {"v": allowed}


# ★ 注入核心:每个危险字符 / 序列都必须被拒,且错误只含变量名不含取值(R5)
@pytest.mark.parametrize(
    "dangerous",
    [
        "a'; DROP TABLE users",  # 单引号 + 分号
        'a" OR "1"="1',  # 双引号
        "a`b",  # 反引号
        "a;b",  # 分号
        "a--comment",  # SQL 行注释序列
        "a/*b",  # SQL 块注释起始
        "a\\b",  # 反斜杠
        "a\nb",  # 换行
        "a b",  # 空格(标识符位有风险,一律禁)
        "1 OR 1=1",  # 经典注入(含空格与 =)
    ],
)
def test_variable_dangerous_value_rejected_name_only_never_value(dangerous: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_workflow_variables({"secret_var": dangerous})
    message = str(exc_info.value)
    assert "unsafe_variable_value" in message
    assert "secret_var" in message
    # ★ R5:错误信息绝不泄露危险取值本身
    assert dangerous not in message


def test_single_hyphen_allowed_double_hyphen_rejected() -> None:
    # 单 `-`(日期)无害;`--`(SQL 注释)必须拦
    assert validate_workflow_variables({"d": "2026-07-07"}) == {"d": "2026-07-07"}
    with pytest.raises(ValueError, match="unsafe_variable_value"):
        validate_workflow_variables({"d": "a--b"})


def test_precedence_builtin_lt_spec_lt_trigger() -> None:
    # 合并优先级:builtin < spec.variables < 触发时;触发覆盖 spec,builtin 不可被覆盖(禁冲突)
    builtin = builtin_when_variables(datetime(2026, 7, 7, tzinfo=UTC))
    spec_vars = validate_workflow_variables({"env": "staging", "region": "ap-east-1"})
    trigger_vars = validate_workflow_variables({"env": "prod"})
    merged = {**builtin, **spec_vars, **trigger_vars}
    assert merged["env"] == "prod"  # 触发 > spec
    assert merged["region"] == "ap-east-1"  # spec 保留
    assert merged["today"] == builtin["today"]  # builtin 不被覆盖


# ---------------------------------------------------------------- C-7 PR3:list 型变量


def test_list_variable_accepted_and_roundtrips_on_spec() -> None:
    spec = WorkflowSpec.model_validate(
        {
            "nodes": [make_node("n1")],
            "edges": [],
            "variables": {"ids": ["1", "2", "3"], "env": "prod"},
        }
    )
    assert spec.variables == {"ids": ["1", "2", "3"], "env": "prod"}
    assert WorkflowSpec.model_validate(spec.model_dump()) == spec


def test_list_variable_empty_list_accepted() -> None:
    assert validate_workflow_variables({"ids": []}) == {"ids": []}


def test_list_variable_element_failing_charset_rejected_name_only() -> None:
    # 单个元素含危险字符 → 拒绝;错误只含变量名,绝不含元素值(R5)
    with pytest.raises(ValueError) as exc_info:
        validate_workflow_variables({"ids": ["ok", "x'; DROP TABLE t"]})
    message = str(exc_info.value)
    assert "unsafe_variable_value" in message
    assert "ids" in message
    assert "DROP TABLE" not in message


def test_list_variable_non_str_element_rejected_name_only() -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_workflow_variables({"ids": ["1", 2]})
    assert "invalid_variable_value" in str(exc_info.value)
    assert "ids" in str(exc_info.value)


def test_list_variable_over_length_cap_rejected() -> None:
    ok = {"ids": ["x"] * MAX_VARIABLE_LIST_LENGTH}
    assert validate_workflow_variables(ok) == ok
    too_long = {"ids": ["x"] * (MAX_VARIABLE_LIST_LENGTH + 1)}
    with pytest.raises(ValueError, match="variable_list_too_long"):
        validate_workflow_variables(too_long)


def test_list_variable_element_length_cap_enforced() -> None:
    long_element = "a" * (MAX_VARIABLE_VALUE_LENGTH + 1)
    with pytest.raises(ValueError, match="variable_value_too_long"):
        validate_workflow_variables({"ids": [long_element]})


# ── C-10 SensorTrigger 构造期校验 ────────────────────────────────────────────


def _sensor(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "sql": "SELECT COUNT(*) FROM arrivals",
        "datasource_id": "ds-1",
        "check_interval_seconds": 60,
        "cooldown_seconds": 300,
    }
    payload.update(overrides)
    return payload


def test_sensor_valid_roundtrip() -> None:
    spec = make_spec(["only"], [], sensor=_sensor())
    assert spec.sensor is not None
    assert spec.sensor.datasource_id == "ds-1"
    assert spec.sensor.enabled is True  # 默认开
    # 往返序列化保留 sensor
    assert WorkflowSpec.model_validate(spec.model_dump(mode="json")).sensor == spec.sensor


def test_sensor_interval_below_minimum_rejected() -> None:
    with pytest.raises(ValidationError):
        make_spec(["only"], [], sensor=_sensor(check_interval_seconds=1))


def test_sensor_negative_cooldown_rejected() -> None:
    with pytest.raises(ValidationError):
        make_spec(["only"], [], sensor=_sensor(cooldown_seconds=-1))


def test_sensor_zero_cooldown_allowed() -> None:
    spec = make_spec(["only"], [], sensor=_sensor(cooldown_seconds=0))
    assert spec.sensor is not None
    assert spec.sensor.cooldown_seconds == 0


def test_sensor_blank_datasource_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid_sensor_datasource"):
        make_spec(["only"], [], sensor=_sensor(datasource_id="   "))


def test_sensor_empty_sql_rejected() -> None:
    with pytest.raises(ValidationError):
        make_spec(["only"], [], sensor=_sensor(sql=""))
