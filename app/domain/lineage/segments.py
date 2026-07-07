"""L-3 存储过程深度解析 domain 层:控制流感知切段 + 业务标题 + 动态 SQL 检测。

纯正则 + 字符串扫描 + sqlglot AST,无 IO / 无 DB(R1:domain 层禁 DB 驱动)。
移植自 1.x ``app/lineage/segments.py`` 已验证逻辑(设计稿 L-3 §一 A1/A2/A3/A5),
接在 ``parser.py:_parse_or_split_plsql`` 这个缝上。本模块只做**切段与元数据抽取**,
不建补链边(cursor/UDF/BULK/TRIGGER 补链是 PR2)。

对外入口:
- :func:`extract_procedure_segments` —— 把 CREATE PROCEDURE/FUNCTION/PACKAGE BODY/
  TRIGGER / 匿名 ``DECLARE...BEGIN`` 块内嵌套的 DML 逐条抽出,带 procedure_name /
  kind / segment_index / sql / line_start / line_end / preceding_comment /
  parse_status / cursor_sources / trigger_source。
- :func:`extract_dynamic_sql_segments` —— EXECUTE IMMEDIATE / PREPARE / 变量拼接
  三条精确路径,尽力解析 + unresolved 占位告警。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _BodySegment:
    """过程体内一段 DML,带原文位置和前置注释。

    text          —— 段落原始文本(DML 关键字起,控制流壳子已剥;业务标题保留)。
    dml_start     —— DML 关键字(INSERT/UPDATE/...)在 body 中的起始 offset,
                     当作"语义起始行"—— business title 注释不计入。
    end           —— 段落(含 ;)在 body 中的结束 offset。
    preceding_comment —— 提取出来的业务标题注释(已 strip ``--`` / ``/* */`` 标记),无则空串。
    cursor_sources —— 段来自 ``FOR rec IN (SELECT ... FROM tables) LOOP <body>`` 时,
                     记录 cursor SELECT 引用的源表(供 PR2 补链消费)。
    """

    text: str
    dml_start: int
    end: int
    preceding_comment: str
    cursor_sources: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 过程头 / 块边界正则(设计稿 A1)
# ---------------------------------------------------------------------------

_RE_PROC_HEADER = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:DEFINER\s*=\s*\S+\s+)?"
    r"(?P<kind>PROCEDURE|FUNCTION|PACKAGE\s+BODY|TRIGGER)\s+(?P<name>[\w$#.\"`\[\]]+)",
    flags=re.IGNORECASE,
)
# TRIGGER 头部 `[BEFORE|AFTER|INSTEAD OF] <event(s)> ON <table>` 的触发源表。
_RE_TRIGGER_SOURCE = re.compile(
    r"\b(?:BEFORE|AFTER|INSTEAD\s+OF)\s+(?:[\w]+(?:\s+OR\s+[\w]+)*)\s+"
    r"(?:OF\s+[\w$#,\s]+\s+)?ON\s+([\w$#.\"`\[\]]+)",
    flags=re.IGNORECASE,
)
# 顶层匿名 PL/SQL 块 `DECLARE ... BEGIN ... END;` / `BEGIN ... END;`。没有 CREATE
# 前缀,否则整个脚本会因 DECLARE 行让 sqlglot 解析失败。当 anonymous procedure 处理。
_RE_ANON_PLSQL_BLOCK = re.compile(
    r"(?m)^[\s]*(DECLARE\b[\s\S]*?\bBEGIN\b|BEGIN\b)",
    flags=re.IGNORECASE,
)
# 嵌套在 PACKAGE BODY 里的 PROCEDURE / FUNCTION 没有 CREATE 前缀。只在已知 PACKAGE
# BODY block 范围内用 —— 否则随处一段 `PROCEDURE foo IS` 都会误识别。
_RE_NESTED_PROC_HEADER = re.compile(
    r"\b(?P<kind>PROCEDURE|FUNCTION)\s+(?P<name>[\w$#.\"`\[\]]+)\b",
    flags=re.IGNORECASE,
)
_RE_BEGIN = re.compile(r"\bBEGIN\b", flags=re.IGNORECASE)
_RE_BLOCK_TOKEN = re.compile(r"\bBEGIN\b|\bEND\b", flags=re.IGNORECASE)

# 块级 END 必须是 `END;`、`END proc_name;`、`END$` 或行末。`END IF;` / `END LOOP;`
# / `END CASE;` 和裸 case 表达式里的 `end` 是 PL/SQL 控制流,不算 block 结束。
_CONTROL_END_KEYWORDS = frozenset({"IF", "LOOP", "CASE", "RECORD", "FOR", "WHILE", "XMLELEMENT"})


def _is_block_end(body: str, pos: int) -> bool:
    """从 `END` 关键字后面的位置判断是不是 block-level 结束。

    block-level:`END;`、`END identifier;`(identifier 不属于控制流关键字)。
    控制流:`END IF;` / `END LOOP;` / `END CASE;` / 裸 `case when ... end` 等。
    """
    while pos < len(body) and body[pos].isspace():
        pos += 1
    if pos >= len(body):
        return True  # 文件末尾的裸 END 也当 block 结束(一般没分号)
    if body[pos] == ";":
        return True
    if not (body[pos].isalpha() or body[pos] == "_"):
        return False
    start = pos
    while pos < len(body) and (body[pos].isalnum() or body[pos] == "_"):
        pos += 1
    ident = body[start:pos].upper()
    if ident in _CONTROL_END_KEYWORDS:
        return False
    while pos < len(body) and body[pos].isspace():
        pos += 1
    return pos < len(body) and body[pos] == ";"


_RE_BODY_DML = re.compile(
    r"\b(WITH|SELECT|INSERT|REPLACE\s+INTO|UPDATE|DELETE|MERGE|"
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:GLOBAL\s+TEMPORARY\s+|TEMPORARY\s+|TEMP\s+)?TABLE|"
    r"TRUNCATE)\b",
    flags=re.IGNORECASE,
)

# Cursor FOR loop 头部:`FOR rec IN (SELECT ... ) LOOP <body>` —— LOOP 之前的部分
# 整体是控制流壳子。要从 LOOP 关键字之后才开始找真正的 DML。
_RE_CURSOR_FOR_LOOP_HEAD = re.compile(r"^\s*FOR\s+[\w$#]+\s+IN\b", flags=re.IGNORECASE)
_RE_LOOP_KEYWORD = re.compile(r"\bLOOP\b", flags=re.IGNORECASE)
# 扫整个 body 找 cursor FOR loop 范围用(非锚定开头)。
_RE_CURSOR_FOR_LOOP_INLINE = re.compile(r"\bFOR\s+[\w$#]+\s+IN\b", flags=re.IGNORECASE)
# 显式 CURSOR 声明 `CURSOR <name> [(参数)] [IS|AS] SELECT ...;`。
_RE_CURSOR_DECL = re.compile(
    r"\bCURSOR\s+([A-Za-z_][\w$#]*)\s*(?:\([^)]*\))?\s+(?:IS|AS)\s+",
    flags=re.IGNORECASE,
)


def _collect_cursor_decls(body: str) -> dict[str, list[str]]:
    """扫 body 找所有 ``CURSOR <name> IS SELECT ...;`` 声明。

    返回 ``{cursor_name_lower: [source_tables]}``;``FOR rec IN <cursor_name> LOOP``
    这种引用形式靠这个映射回填 cursor_sources。
    """
    decls: dict[str, list[str]] = {}
    pos = 0
    length = len(body)
    while pos < length:
        m = _RE_CURSOR_DECL.search(body, pos)
        if not m:
            break
        cursor_name = m.group(1)
        select_start = m.end()
        # 找匹配的 ; 在顶层(depth=0)。SELECT 子查询里可能有 ( ) 嵌套
        depth = 0
        j = select_start
        while j < length:
            ch = body[j]
            if ch in "'\"":
                quote = ch
                j += 1
                while j < length:
                    if body[j] == quote:
                        if j + 1 < length and body[j + 1] == quote:
                            j += 2
                            continue
                        j += 1
                        break
                    j += 1
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == ";" and depth == 0:
                break
            j += 1
        select_text = body[select_start:j]
        tables = _extract_cursor_select_tables(select_text)
        if tables:
            decls[cursor_name.lower()] = tables
        pos = j + 1 if j < length else length
    return decls


def _collect_loop_scopes(
    body: str, extra_cursor_decls: dict[str, list[str]] | None = None
) -> list[tuple[int, int, list[str]]]:
    """扫 body 找所有 ``FOR rec IN (...) LOOP ... END LOOP;`` 范围。

    返回 ``[(scope_start, scope_end, cursor_sources)]``。给 body 内被 ``;`` 切碎、
    落在 LOOP 体内但丢了上下文的多个 DML 段统一补同一份 cursor_sources。嵌套 LOOP
    都独立记录,取最内层。字符串内的 LOOP/END LOOP 关键字会被字符串 skip 跳过。
    """
    scopes: list[tuple[int, int, list[str]]] = []
    cursor_decls = _collect_cursor_decls(body)
    if extra_cursor_decls:
        for name, tables in extra_cursor_decls.items():
            cursor_decls.setdefault(name, list(tables))
    pos = 0
    length = len(body)
    while pos < length:
        m = _RE_CURSOR_FOR_LOOP_INLINE.search(body, pos)
        if not m:
            break
        for_start = m.start()
        cursor_in_end = m.end()
        # 跳空白找 cursor SELECT 的 ( 或 cursor 名字
        j = cursor_in_end
        while j < length and body[j].isspace():
            j += 1
        cursor_sources: list[str] = []
        if j < length and body[j] == "(":
            # 内联 SELECT 形式:FOR rec IN (SELECT ...) LOOP
            depth = 1
            select_start = j + 1
            j += 1
            select_end = select_start
            while j < length and depth > 0:
                ch = body[j]
                if ch in "'\"":
                    quote = ch
                    j += 1
                    while j < length:
                        if body[j] == quote:
                            if j + 1 < length and body[j + 1] == quote:
                                j += 2
                                continue
                            j += 1
                            break
                        j += 1
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        select_end = j
                        j += 1
                        break
                j += 1
            if depth != 0:
                pos = cursor_in_end
                continue
            cursor_sources = _extract_cursor_select_tables(body[select_start:select_end])
        elif j < length and (body[j].isalpha() or body[j] == "_"):
            # 显式 cursor 名字引用形式 FOR rec IN cur_name LOOP,可能带调用参数 (...)
            name_start = j
            while j < length and (body[j].isalnum() or body[j] in "_$#."):
                j += 1
            cursor_name = body[name_start:j].lower()
            cursor_sources = list(cursor_decls.get(cursor_name, []))
            while j < length and body[j].isspace():
                j += 1
            if j < length and body[j] == "(":
                arg_depth = 1
                j += 1
                while j < length and arg_depth > 0:
                    ch = body[j]
                    if ch in "'\"":
                        quote = ch
                        j += 1
                        while j < length:
                            if body[j] == quote:
                                if j + 1 < length and body[j + 1] == quote:
                                    j += 2
                                    continue
                                j += 1
                                break
                            j += 1
                        continue
                    if ch == "(":
                        arg_depth += 1
                    elif ch == ")":
                        arg_depth -= 1
                    j += 1
            # 后面可能有 reverse / 数字范围(FOR i IN 1..N LOOP)—— 不是 cursor,
            # 没声明就当无 source 处理,不阻断 LOOP 范围识别
        else:
            pos = cursor_in_end
            continue
        # 跳空白找 LOOP 关键字
        while j < length and body[j].isspace():
            j += 1
        if body[j : j + 4].upper() != "LOOP" or (j + 4 < length and body[j + 4].isalnum()):
            pos = cursor_in_end
            continue
        # 从 LOOP 之后找匹配的 END LOOP(按 LOOP / END LOOP 配对,跳字符串)
        k = j + 4
        loop_depth = 1
        while k < length and loop_depth > 0:
            ch = body[k]
            if ch in "'\"":
                quote = ch
                k += 1
                while k < length:
                    if body[k] == quote:
                        if k + 1 < length and body[k + 1] == quote:
                            k += 2
                            continue
                        k += 1
                        break
                    k += 1
                continue
            if ch.isalpha():
                # END LOOP(先 END 后 LOOP)
                if body[k : k + 3].upper() == "END" and (
                    k + 3 >= length or not body[k + 3].isalnum()
                ):
                    nx = k + 3
                    while nx < length and body[nx].isspace():
                        nx += 1
                    if body[nx : nx + 4].upper() == "LOOP" and (
                        nx + 4 >= length or not body[nx + 4].isalnum()
                    ):
                        loop_depth -= 1
                        k = nx + 4
                        while k < length and body[k].isspace():
                            k += 1
                        if k < length and body[k] == ";":
                            k += 1
                        continue
                # 嵌套 LOOP:单独 LOOP 关键字也增加 depth
                if body[k : k + 4].upper() == "LOOP" and (
                    k + 4 >= length or not body[k + 4].isalnum()
                ):
                    loop_depth += 1
                    k += 4
                    continue
            k += 1
        scope_end = k
        scopes.append((for_start, scope_end, cursor_sources))
        # 同一外层 LOOP 内部还可能嵌另一层 cursor FOR;从 LOOP 关键字后继续扫
        pos = j + 4
    return scopes


# 剥 cursor FOR 头部时,顺便从 cursor SELECT 里抽源表 —— 供 PR2 补链消费。
_RE_FROM_TABLE = re.compile(
    r"\bFROM\s+([\w$#.\"`\[\]]+(?:\s*,\s*[\w$#.\"`\[\]]+)*)",
    flags=re.IGNORECASE,
)
_RE_JOIN_TABLE = re.compile(r"\bJOIN\s+([\w$#.\"`\[\]]+)", flags=re.IGNORECASE)


def _extract_cursor_select_tables(cursor_select: str) -> list[str]:
    """轻量级提取 cursor SELECT 子查询的源表(不走 sqlglot,只取顶层 FROM/JOIN 标识符)。"""
    tables: list[str] = []
    seen: set[str] = set()

    def _push(raw: str) -> None:
        name = raw.strip().strip('"`[]')
        if (
            not name
            or name.startswith("(")
            or name.upper()
            in {
                "WHERE",
                "GROUP",
                "ORDER",
                "HAVING",
                "LIMIT",
                "FETCH",
            }
        ):
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        tables.append(name)

    for m in _RE_FROM_TABLE.finditer(cursor_select):
        for piece in m.group(1).split(","):
            _push(piece.split()[0] if piece.split() else "")
    for m in _RE_JOIN_TABLE.finditer(cursor_select):
        _push(m.group(1))
    return tables


def _strip_cursor_for_prefix_with_sources(seg_text: str) -> tuple[str, list[str]]:
    """段以 ``FOR x IN (...) LOOP`` 开头时剥掉头部,返回 ``(LOOP 后内容, cursor 源表)``。

    其他情况返回 ``(原 seg_text, [])``。剥掉时同步括号配平 —— cursor 子查询含括号。
    """
    if not _RE_CURSOR_FOR_LOOP_HEAD.match(seg_text):
        return seg_text, []
    depth = 0
    pos = 0
    length = len(seg_text)
    cursor_select_start = -1
    cursor_select_end = -1
    while pos < length:
        ch = seg_text[pos]
        if ch in "'\"":
            quote = ch
            pos += 1
            while pos < length:
                if seg_text[pos] == quote and (pos + 1 >= length or seg_text[pos + 1] != quote):
                    pos += 1
                    break
                pos += 1
            continue
        if ch == "(":
            if depth == 0:
                cursor_select_start = pos + 1
            depth += 1
            pos += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0 and cursor_select_end == -1:
                cursor_select_end = pos
            pos += 1
            continue
        if depth == 0 and ch.isalpha():
            m = _RE_LOOP_KEYWORD.match(seg_text, pos)
            if m and (m.end() >= length or not seg_text[m.end()].isalnum()):
                cursor_sources: list[str] = []
                if 0 <= cursor_select_start < cursor_select_end:
                    cursor_sources = _extract_cursor_select_tables(
                        seg_text[cursor_select_start:cursor_select_end]
                    )
                return seg_text[m.end() :].lstrip(), cursor_sources
        pos += 1
    return seg_text, []


# 纯空白 + 行注释(`--`) + 块注释(`/* */`)的组合。fullmatch 这个表示 DML 前面
# 没有控制流壳子(IF/THEN、CASE 等),可以原样保留 —— 业务标题就在前缀注释里。
_RE_PURE_COMMENT_PREFIX = re.compile(r"(?:\s|--[^\n]*|/\*(?:[^*]|\*(?!/))*\*/)*")


def extract_procedure_segments(sql: str) -> list[dict[str, Any]]:
    """抽 CREATE PROCEDURE/FUNCTION/PACKAGE BODY/TRIGGER / 匿名块内嵌套的 DML。

    靠 token-balanced 扫描跳过控制流壳子(IF/LOOP/EXCEPTION)与嵌套 BEGIN/END 块。

    段落保留原始换行 —— ``-- 行注释`` 必须靠换行终止,否则会把后面的列名一起吞掉。
    用规范化文本(空白压平)做 dedupe key,原始格式喂给 sqlglot 解析。

    每条记录:procedure_name / procedure_kind / segment_index / sql / confidence /
    line_start / line_end / preceding_comment / parse_status / cursor_sources /
    trigger_source。``parse_status`` 默认 'unknown',由上游(parser.py)跑完 sqlglot
    后回填 'parsed' / 'unsupported'。
    """
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    # 先扫顶层匿名 PL/SQL 块(DECLARE ... BEGIN / 单独 BEGIN)。不落在任何 CREATE
    # 范围内的算 anonymous,当 procedure_kind=ANONYMOUS 处理。
    create_ranges: list[tuple[int, int]] = []
    for cm in _RE_PROC_HEADER.finditer(sql):
        nxt = _RE_PROC_HEADER.search(sql, cm.end())
        create_ranges.append((cm.start(), nxt.start() if nxt else len(sql)))
    for am in _RE_ANON_PLSQL_BLOCK.finditer(sql):
        anon_start = am.start()
        if any(s <= anon_start < e for s, e in create_ranges):
            continue
        begin_in_match = re.search(r"\bBEGIN\b", am.group(0), flags=re.IGNORECASE)
        if not begin_in_match:
            continue
        body_start_pos = am.start() + begin_in_match.end()
        _, body_end_pos = _find_block_end(sql, body_start_pos, depth=1)
        decl_region = sql[anon_start : body_start_pos - len("BEGIN")]
        body = sql[body_start_pos:body_end_pos]
        for index, item in enumerate(_iter_procedure_body_segments(body, decl_region), start=1):
            preserved = clean_procedure_segment(item.text)
            if not preserved:
                continue
            dedupe_key = " ".join(preserved.split())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            abs_start = body_start_pos + item.dml_start
            abs_end = body_start_pos + item.end
            result.append(
                {
                    "procedure_name": "<anonymous>",
                    "procedure_kind": "ANONYMOUS",
                    "segment_index": str(index),
                    "sql": preserved,
                    "confidence": "high",
                    "line_start": _line_of(sql, abs_start),
                    "line_end": _line_of(sql, abs_end),
                    "preceding_comment": item.preceding_comment,
                    "parse_status": "unknown",
                    "cursor_sources": list(item.cursor_sources or []),
                    "trigger_source": "",
                }
            )
    # 把 PACKAGE BODY 拆成多个嵌套 procedure scope,每个独立处理。
    for outer_header in _RE_PROC_HEADER.finditer(sql):
        outer_name = outer_header.group("name").strip()
        outer_kind = " ".join(outer_header.group("kind").split()).upper()
        pkg_declaration_region = ""
        if outer_kind == "PACKAGE BODY":
            # PACKAGE BODY 自身没有 BEGIN/END 配对(IS/AS ... END pkg; 直接收尾)。
            # 范围从 header 到下一个 CREATE PROCEDURE/FUNCTION/PACKAGE 或文件末尾。
            next_outer = _RE_PROC_HEADER.search(sql, outer_header.end())
            pkg_body_end = next_outer.start() if next_outer else len(sql)
            first_nested = _RE_NESTED_PROC_HEADER.search(sql, outer_header.end(), pkg_body_end)
            pkg_decl_end = first_nested.start() if first_nested else pkg_body_end
            pkg_declaration_region = sql[outer_header.end() : pkg_decl_end]
            scopes = _find_nested_proc_scopes(sql, outer_header.end(), pkg_body_end, outer_name)
            if not scopes:
                scopes = [
                    (
                        outer_name,
                        outer_kind,
                        outer_header.end(),
                        pkg_body_end,
                        pkg_declaration_region,
                    )
                ]
        else:
            # 普通 PROCEDURE / FUNCTION / TRIGGER:单个 scope
            body_start = _RE_BEGIN.search(sql, pos=outer_header.end())
            if not body_start:
                continue
            _, body_end = _find_block_end(sql, body_start.end(), depth=1)
            scopes = [
                (
                    outer_name,
                    outer_kind,
                    body_start.end(),
                    body_end,
                    sql[outer_header.end() : body_start.start()],
                )
            ]

        trigger_source = ""
        if outer_kind == "TRIGGER":
            header_text = sql[outer_header.end() : outer_header.end() + 500]
            ts_match = _RE_TRIGGER_SOURCE.search(header_text)
            if ts_match:
                trigger_source = ts_match.group(1).strip().strip('"`[]')
        for name, kind, scope_body_start, scope_body_end, declaration_region in scopes:
            if outer_kind == "PACKAGE BODY":
                merged_decl = pkg_declaration_region + "\n" + declaration_region
            else:
                merged_decl = declaration_region
            body = sql[scope_body_start:scope_body_end]
            body_offset = scope_body_start
            for index, item in enumerate(_iter_procedure_body_segments(body, merged_decl), start=1):
                preserved = clean_procedure_segment(item.text)
                if not preserved:
                    continue
                dedupe_key = " ".join(preserved.split())
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                abs_start = body_offset + item.dml_start
                abs_end = body_offset + item.end
                result.append(
                    {
                        "procedure_name": name,
                        "procedure_kind": kind,
                        "segment_index": str(index),
                        "sql": preserved,
                        "confidence": "high",
                        "line_start": _line_of(sql, abs_start),
                        "line_end": _line_of(sql, abs_end),
                        "preceding_comment": item.preceding_comment,
                        "parse_status": "unknown",
                        "cursor_sources": list(item.cursor_sources or []),
                        "trigger_source": trigger_source,
                    }
                )
    return result


def _find_block_end(sql: str, start_pos: int, depth: int = 0) -> tuple[int, int]:
    """从 start_pos 找 PL/SQL 块的结束位置,返回 (matched_begin_pos, end_pos)。

    depth=0 时,调用方在 IS/AS 之后;先 skip 到第一个 BEGIN,然后配对 END。
    depth=1 时,已经 skip 过 BEGIN 了,直接配对 END。
    """
    if depth == 0:
        begin_match = _RE_BEGIN.search(sql, pos=start_pos)
        if not begin_match:
            return -1, len(sql)
        matched_begin = begin_match.start()
        cursor = begin_match.end()
        depth = 1
    else:
        matched_begin = start_pos
        cursor = start_pos
    for tok_match in _RE_BLOCK_TOKEN.finditer(sql, pos=cursor):
        tok = tok_match.group(0).upper()
        if tok == "BEGIN":
            depth += 1
        else:
            if not _is_block_end(sql, tok_match.end()):
                continue
            depth -= 1
            if depth == 0:
                return matched_begin, tok_match.start()
    return matched_begin, len(sql)


def _find_nested_proc_scopes(
    sql: str, start: int, end: int, parent_name: str
) -> list[tuple[str, str, int, int, str]]:
    """扫 PACKAGE BODY 范围内所有嵌套 PROCEDURE/FUNCTION。

    返回每个的 (qualified_name, kind, body_start_offset, body_end_offset,
    declaration_region)。每个嵌套 proc 用其自己的 IS/AS → BEGIN 之间作 declaration_region。
    """
    out: list[tuple[str, str, int, int, str]] = []
    pos = start
    while pos < end:
        m = _RE_NESTED_PROC_HEADER.search(sql, pos, end)
        if not m:
            break
        kind = m.group("kind").upper()
        local_name = m.group("name").strip()
        body_start_match = _RE_BEGIN.search(sql, m.end(), end)
        if not body_start_match:
            pos = m.end()
            continue
        scope_decl_region = sql[m.end() : body_start_match.start()]
        _body_begin, body_end = _find_block_end(sql, body_start_match.end(), depth=1)
        if body_end > end:
            body_end = end
        qualified = (
            f"{parent_name}.{local_name}"
            if parent_name and local_name not in parent_name
            else local_name
        )
        out.append((qualified, kind, body_start_match.end(), body_end, scope_decl_region))
        pos = body_end + 1
    return out


def _line_of(text: str, offset: int) -> int:
    """1-based line number of `offset` in `text`. Beyond-end → last line."""
    if offset <= 0:
        return 1
    capped = min(offset, len(text))
    return text.count("\n", 0, capped) + 1


def clean_procedure_segment(segment: str) -> str:
    """Strip 首尾空白与末尾 ``;``,但**保留**换行。

    ``-- 行注释`` 必须靠换行终止;若把所有空白压成单空格,注释会一直吞到行末,导致
    sqlglot 把列名 / 表达式都识别成注释、括号失配。
    """
    return segment.strip().rstrip(";").strip()


def _iter_procedure_body_segments(body: str, declaration_region: str = "") -> list[_BodySegment]:
    """把一个过程 body 按顶层 ``;`` 切段,跳过控制流壳子,返回带 body-内偏移的段。

    declaration_region 是过程头部 ``IS/AS ... BEGIN`` 之间的区域文本,用于抽显式
    CURSOR 声明,供 LOOP 范围扫描回填 cursor_sources。
    """
    raw_segments: list[tuple[str, int, int]] = []
    depth = 0
    buffer: list[str] = []
    seg_start = 0
    pos = 0
    length = len(body)
    while pos < length:
        char = body[pos]
        # 跳字符串字面量,避免字符串内 ; 误切。
        if char in "'\"":
            quote = char
            buffer.append(char)
            pos += 1
            while pos < length:
                buffer.append(body[pos])
                if body[pos] == quote and (pos + 1 >= length or body[pos + 1] != quote):
                    pos += 1
                    break
                if body[pos] == quote:
                    buffer.append(body[pos + 1])
                    pos += 2
                    continue
                pos += 1
            continue
        # 跟踪嵌套 BEGIN/END depth,避免嵌套块内 ; 结束外层段。
        if char.isalpha():
            tail = body[pos : pos + 6].upper()
            if tail.startswith("BEGIN") and (pos + 5 >= length or not body[pos + 5].isalnum()):
                depth += 1
                buffer.append(body[pos : pos + 5])
                pos += 5
                continue
            if tail.startswith("END") and (pos + 3 >= length or not body[pos + 3].isalnum()):
                # 同 _is_block_end 逻辑:仅 block-level END 参与 depth 计数。
                if _is_block_end(body, pos + 3) and depth > 0:
                    depth -= 1
                buffer.append(body[pos : pos + 3])
                pos += 3
                continue
        if char == ";" and depth == 0:
            raw_segments.append(("".join(buffer), seg_start, pos + 1))
            buffer = []
            pos += 1
            seg_start = pos
            continue
        buffer.append(char)
        pos += 1
    tail_text = "".join(buffer)
    if tail_text.strip():
        raw_segments.append((tail_text, seg_start, length))

    cleaned: list[_BodySegment] = []
    for raw_text, raw_start, raw_end in raw_segments:
        leading_ws = len(raw_text) - len(raw_text.lstrip())
        seg_text_orig = raw_text.strip()
        if not seg_text_orig:
            continue
        # 段可能以控制流壳子(IF ... THEN / FOR ... LOOP)开头。找首个 DML 关键字。
        # 特殊处理 `FOR rec IN (SELECT ...) LOOP <DML>`:剥 cursor 头部,从 LOOP 后找。
        seg_text, cursor_sources = _strip_cursor_for_prefix_with_sources(seg_text_orig)
        cursor_strip = len(seg_text_orig) - len(seg_text)
        match = _RE_BODY_DML.search(seg_text)
        if not match:
            continue
        prefix = seg_text[: match.start()]
        # 若 DML 前面只有空白和 line/block 注释(业务标题),保留;反之(控制流壳子)剥掉。
        if _RE_PURE_COMMENT_PREFIX.fullmatch(prefix):
            kept_text = seg_text
            preceding_comment = _strip_comment_prefix(prefix)
        else:
            kept_text = seg_text[match.start() :]
            preceding_comment = ""
        kept_offset_in_seg = len(seg_text) - len(kept_text)
        body_kept_start = raw_start + leading_ws + cursor_strip + kept_offset_in_seg
        if preceding_comment:
            dml_offset = raw_start + leading_ws + cursor_strip + match.start()
        else:
            dml_offset = body_kept_start
        cleaned.append(
            _BodySegment(
                text=kept_text.strip(),
                dml_start=dml_offset,
                end=raw_end,
                preceding_comment=preceding_comment,
                cursor_sources=cursor_sources,
            )
        )
    # 扫整个 body 找 cursor FOR LOOP 范围,给已 split 出来但落在 LOOP 体内、丢了
    # 上下文的多 DML 段补 cursor_sources(取最内层 scope)。
    extra_decls = _collect_cursor_decls(declaration_region) if declaration_region else {}
    scopes = _collect_loop_scopes(body, extra_decls)
    if scopes:
        for seg in cleaned:
            if seg.cursor_sources:
                continue
            inherited: list[str] = []
            for scope_start, scope_end, scope_sources in scopes:
                if scope_start <= seg.dml_start < scope_end and scope_sources:
                    inherited = scope_sources
            if inherited:
                seg.cursor_sources = list(inherited)
    return cleaned


_RE_LINE_COMMENT = re.compile(r"--[^\n]*")
_RE_BLOCK_COMMENT = re.compile(r"/\*(?:[^*]|\*(?!/))*\*/", flags=re.DOTALL)


def _strip_comment_prefix(prefix: str) -> str:
    """从一段纯注释前缀里抽业务标题:剥 ``--`` / ``/* */`` 标记,多行用空格连,截断 200 字符。"""
    pieces: list[str] = []
    for m in _RE_BLOCK_COMMENT.finditer(prefix):
        body = m.group(0)[2:-2].strip().strip("*").strip()
        if body:
            pieces.append(body)
    line_only = _RE_BLOCK_COMMENT.sub(" ", prefix)
    for m in _RE_LINE_COMMENT.finditer(line_only):
        body = m.group(0).lstrip("-").strip()
        if body:
            pieces.append(body)
    if not pieces:
        return ""
    joined = " ".join(pieces).strip()
    if len(joined) > 200:
        joined = joined[:200].rstrip() + "…"
    return joined


# ---------------------------------------------------------------------------
# 动态 SQL(设计稿 A5):尽力解析 + 告警,三条精确路径,不做字面量兜底
# ---------------------------------------------------------------------------


def extract_dynamic_sql_segments(sql: str) -> list[dict[str, str]]:
    """抽动态 SQL:EXECUTE IMMEDIATE / sp_executesql 字面量、PREPARE、变量拼接。

    无法解析的变量(参数/包变量/外部传入)出占位段 ``-- unresolved ...``,
    confidence=``unresolved``,触发 dynamic_sql warning。不做字面量兜底(大过程里
    会产生海量误报)。
    """
    seen: set[str] = set()
    result: list[dict[str, str]] = []

    def add(segment: str, source: str, confidence: str) -> None:
        cleaned = clean_dynamic_segment(segment)
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        result.append({"sql": cleaned, "source": source, "confidence": confidence})

    keyword_pattern = re.compile(
        r"(?:execute\s+immediate|exec(?:ute)?\s+(?:sys\.)?sp_executesql)\s+(?:N)?'((?:''|[^'])*)'",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in keyword_pattern.finditer(sql):
        add(_unescape_sql_string(match.group(1)), "execute_keyword", "high")

    # MySQL: SET @sql := '...'; PREPARE stmt FROM @sql; EXECUTE stmt;
    set_vars = _capture_session_var_assignments(sql)
    prepare_pattern = re.compile(
        r"\bPREPARE\s+\w+\s+FROM\s+(?:@(\w+)|(?:N)?'((?:''|[^'])*)')",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in prepare_pattern.finditer(sql):
        var_name, literal = match.group(1), match.group(2)
        if literal:
            add(_unescape_sql_string(literal), "prepare_literal", "high")
        elif var_name and var_name.lower() in set_vars:
            for assignment in set_vars[var_name.lower()]:
                add(assignment["sql"], "prepare_var", assignment["confidence"])

    # Oracle/PL-SQL: v_sql := 'INSERT INTO ' || p_t || ' SELECT ...'; EXECUTE IMMEDIATE v_sql;
    plsql_vars = _capture_plsql_var_assignments(sql)
    immediate_var_pattern = re.compile(
        r"\bEXECUTE\s+IMMEDIATE\s+([\w$#]+)\s*[;\n]",
        flags=re.IGNORECASE,
    )
    unresolved_seen: set[str] = set()
    for match in immediate_var_pattern.finditer(sql):
        var_name = match.group(1).lower()
        if var_name in plsql_vars:
            for assignment in plsql_vars[var_name]:
                add(assignment["sql"], "var_concat", assignment["confidence"])
        elif var_name not in unresolved_seen:
            # 变量来自过程参数 / 包变量 / cursor / 外部传入,脚本里没赋值 —— 静态
            # 分析无法推断 SQL 内容。出占位段触发 dynamic_sql warning。
            unresolved_seen.add(var_name)
            result.append(
                {
                    "sql": f"-- unresolved EXECUTE IMMEDIATE variable: {match.group(1)}",
                    "source": "execute_var_unresolved",
                    "confidence": "unresolved",
                }
            )

    return result


_RE_SET_ASSIGNMENT = re.compile(r"^\s*SET\s+@(\w+)\s*:?=\s*(.+)$", flags=re.IGNORECASE | re.DOTALL)
_RE_PLSQL_ASSIGNMENT = re.compile(r"^\s*([\w$#]+)\s*:=\s*(.+)$", flags=re.IGNORECASE | re.DOTALL)


def _capture_session_var_assignments(sql: str) -> dict[str, list[dict[str, str]]]:
    return _capture_var_assignments(sql, _RE_SET_ASSIGNMENT)


def _capture_plsql_var_assignments(sql: str) -> dict[str, list[dict[str, str]]]:
    return _capture_var_assignments(sql, _RE_PLSQL_ASSIGNMENT)


def _capture_var_assignments(
    sql: str, assignment_re: re.Pattern[str]
) -> dict[str, list[dict[str, str]]]:
    # 剥块壳 token,让过程体内的变量赋值成为顶层语句。
    flat = re.sub(r"\b(BEGIN|END|DECLARE|THEN|ELSE|ELSIF|LOOP)\b", " ", sql, flags=re.IGNORECASE)
    result: dict[str, list[dict[str, str]]] = {}
    for segment in _split_top_level_statements(flat):
        match = assignment_re.match(segment)
        if not match:
            continue
        name = match.group(1).lower()
        rhs = match.group(2).strip()
        if "||" in rhs or rhs.upper().startswith("CONCAT"):
            rebuilt = _rebuild_concat(rhs)
            if rebuilt and _looks_like_lineage_sql(rebuilt):
                result.setdefault(name, []).append({"sql": rebuilt, "confidence": "low"})
            continue
        if rhs.startswith("'") or rhs[:2].upper() == "N'":
            literal = _strip_quoted(rhs)
            if _looks_like_lineage_sql(literal):
                result.setdefault(name, []).append({"sql": literal, "confidence": "high"})
    return result


def _split_top_level_statements(sql: str) -> list[str]:
    """把 SQL 脚本按顶层 ``;`` 切段,跳字符串。"""
    statements: list[str] = []
    buf: list[str] = []
    pos = 0
    length = len(sql)
    while pos < length:
        char = sql[pos]
        if char in "'\"":
            quote = char
            buf.append(char)
            pos += 1
            while pos < length:
                buf.append(sql[pos])
                if sql[pos] == quote and (pos + 1 >= length or sql[pos + 1] != quote):
                    pos += 1
                    break
                if sql[pos] == quote:
                    buf.append(sql[pos + 1])
                    pos += 2
                    continue
                pos += 1
            continue
        if char == ";":
            statements.append("".join(buf).strip())
            buf = []
            pos += 1
            continue
        buf.append(char)
        pos += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s]


def _strip_quoted(value: str) -> str:
    cleaned = value.strip()
    if cleaned[:1].upper() == "N":
        cleaned = cleaned[1:]
    if cleaned.startswith("'") and cleaned.endswith("'"):
        cleaned = cleaned[1:-1]
    return _unescape_sql_string(cleaned)


def _rebuild_concat(expression: str) -> str:
    """把 concat 里的字面量段重组,变量段替换成 ``:placeholder`` 让 sqlglot 可解析。

    处理两种形态:``'lit' || var || 'lit'``(PL/SQL)与 ``CONCAT('lit', var, 'lit')``(MySQL)。
    """
    expression = expression.strip()
    parts: list[str]
    concat_match = re.match(r"CONCAT\s*\((.*)\)\s*$", expression, flags=re.IGNORECASE | re.DOTALL)
    if concat_match:
        parts = _split_top_level(concat_match.group(1), ",")
    else:
        parts = _split_top_level(expression, "||")
    rebuilt: list[str] = []
    for raw in parts:
        item = raw.strip()
        if not item:
            continue
        if item.startswith("'") or item[:2].upper() == "N'":
            rebuilt.append(_strip_quoted(item))
        else:
            placeholder = re.sub(r"\W+", "_", item).strip("_") or "var"
            rebuilt.append(f":{placeholder}")
    return "".join(rebuilt)


def _split_top_level(expression: str, delimiter: str) -> list[str]:
    """按 delimiter 切分,尊重括号与字符串。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    pos = 0
    delim_len = len(delimiter)
    while pos < len(expression):
        char = expression[pos]
        if char == "'":
            buf.append(char)
            pos += 1
            while pos < len(expression):
                buf.append(expression[pos])
                if expression[pos] == "'" and (
                    pos + 1 >= len(expression) or expression[pos + 1] != "'"
                ):
                    pos += 1
                    break
                if expression[pos] == "'":
                    buf.append(expression[pos + 1])
                    pos += 2
                    continue
                pos += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth == 0 and expression[pos : pos + delim_len] == delimiter:
            parts.append("".join(buf))
            buf = []
            pos += delim_len
            continue
        buf.append(char)
        pos += 1
    parts.append("".join(buf))
    return parts


def _unescape_sql_string(value: str) -> str:
    return value.replace("''", "'")


def clean_dynamic_segment(segment: str) -> str:
    return " ".join(segment.strip().rstrip(";").split())


def _looks_like_lineage_sql(segment: str) -> bool:
    cleaned = segment.strip()
    if not cleaned:
        return False
    return bool(
        re.match(r"^(with|select|insert)\b", cleaned, flags=re.IGNORECASE)
        or re.search(
            r"\b(insert|replace)\s+into\b.+\bselect\b",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        )
        or re.search(
            r"\bcreate\s+(or\s+replace\s+)?(temporary\s+|temp\s+)?table\b.+\bas\s+select\b",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
