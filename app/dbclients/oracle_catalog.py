"""Oracle 族目录查询 SQL(ALL_* 视图),按驱动的绑定风格渲染。

Oracle(python-oracledb,命名绑定 `:name`)与 OB-Oracle(走 MySQL 协议的 PyMySQL,
`%(name)s`)查的是同一套 ALL_* 视图;SQL 只维护这一份,占位符写成 `{name}`,
由 `CatalogSql.render(paramstyle)` 输出对应驱动的写法(设计稿 §4.3.2)。

模板里不得出现字面量花括号(渲染走 str.format)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Paramstyle = Literal["named", "pyformat"]


@dataclass(frozen=True)
class CatalogSql:
    text: str
    params: tuple[str, ...] = ()

    def render(self, paramstyle: Paramstyle) -> str:
        if paramstyle == "named":
            placeholders = {name: f":{name}" for name in self.params}
        else:
            placeholders = {name: f"%({name})s" for name in self.params}
        return self.text.format(**placeholders)


# ALL_USERS 全列,系统 schema 由调用方按清单排除(SQL 里省一串 NOT IN)。
LIST_SCHEMAS = CatalogSql(
    """
            SELECT USERNAME AS name
            FROM ALL_USERS
            ORDER BY USERNAME
            """
)

# ALL_TABLES UNION ALL_VIEWS(与 DM 一致连视图一起列);同名绑定 owner 用两次。
LIST_TABLES = CatalogSql(
    """
            SELECT OWNER AS schema_name, TABLE_NAME AS name, 'TABLE' AS table_type
            FROM ALL_TABLES
            WHERE OWNER = UPPER({owner})
            UNION ALL
            SELECT OWNER AS schema_name, VIEW_NAME AS name, 'VIEW' AS table_type
            FROM ALL_VIEWS
            WHERE OWNER = UPPER({owner})
            ORDER BY name
            """,
    ("owner",),
)

# COLUMN_ID 只用于排序:OB-Oracle 从 16 起编,不能当序号用。
LIST_COLUMNS = CatalogSql(
    """
            SELECT
                c.COLUMN_NAME AS name,
                c.DATA_TYPE AS data_type,
                c.NULLABLE AS nullable,
                cc.COMMENTS AS "COMMENT"
            FROM ALL_TAB_COLUMNS c
            LEFT JOIN ALL_COL_COMMENTS cc
              ON cc.OWNER = c.OWNER
             AND cc.TABLE_NAME = c.TABLE_NAME
             AND cc.COLUMN_NAME = c.COLUMN_NAME
            WHERE c.OWNER = UPPER({owner}) AND c.TABLE_NAME = UPPER({tbl})
            ORDER BY c.COLUMN_ID
            """,
    ("owner", "tbl"),
)

LIST_INDEXES = CatalogSql(
    """
            SELECT
                i.INDEX_NAME AS name,
                ic.COLUMN_NAME AS column_name,
                i.UNIQUENESS AS uniqueness,
                ic.COLUMN_POSITION AS column_position
            FROM ALL_INDEXES i
            JOIN ALL_IND_COLUMNS ic
              ON ic.INDEX_OWNER = i.OWNER AND ic.INDEX_NAME = i.INDEX_NAME
            WHERE i.TABLE_OWNER = UPPER({owner}) AND i.TABLE_NAME = UPPER({tbl})
            ORDER BY i.INDEX_NAME, ic.COLUMN_POSITION
            """,
    ("owner", "tbl"),
)

PRIMARY_KEY_COLUMNS = CatalogSql(
    """
            SELECT cc.COLUMN_NAME AS name
            FROM ALL_CONSTRAINTS c
            JOIN ALL_CONS_COLUMNS cc
              ON cc.OWNER = c.OWNER AND cc.CONSTRAINT_NAME = c.CONSTRAINT_NAME
            WHERE c.CONSTRAINT_TYPE = 'P'
              AND c.OWNER = UPPER({owner}) AND c.TABLE_NAME = UPPER({tbl})
            """,
    ("owner", "tbl"),
)

PRIMARY_KEY_INDEX_NAMES = CatalogSql(
    """
            SELECT c.INDEX_NAME AS name
            FROM ALL_CONSTRAINTS c
            WHERE c.CONSTRAINT_TYPE = 'P'
              AND c.OWNER = UPPER({owner}) AND c.TABLE_NAME = UPPER({tbl})
              AND c.INDEX_NAME IS NOT NULL
            """,
    ("owner", "tbl"),
)

# 他人 schema 需 SELECT_CATALOG_ROLE,否则 ORA-31603(与表不存在同码)。
TABLE_DDL = CatalogSql(
    "SELECT DBMS_METADATA.GET_DDL('TABLE', UPPER({tbl}), UPPER({owner})) FROM dual",
    ("tbl", "owner"),
)


__all__ = [
    "LIST_COLUMNS",
    "LIST_INDEXES",
    "LIST_SCHEMAS",
    "LIST_TABLES",
    "PRIMARY_KEY_COLUMNS",
    "PRIMARY_KEY_INDEX_NAMES",
    "TABLE_DDL",
    "CatalogSql",
    "Paramstyle",
]
