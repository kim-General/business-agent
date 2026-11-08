"""Conservative AST guard for SQL emitted by our metric compiler only.

This is defense in depth, not a sandbox for arbitrary model/user SQL.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

from .warehouse import VIEW_NAMES

class SQLSafetyError(ValueError):
    pass

ALLOWED_FUNCTIONS = frozenset({"SUM", "COUNT", "AVG", "MIN", "MAX", "ROUND", "COALESCE", "NULLIF", "CAST", "DATE_TRUNC", "TIMESTAMP_TRUNC", "CASE", "IF", "AND", "OR", "NOT", "EXISTS"})
FORBIDDEN_NODES = frozenset({"Insert", "Update", "Delete", "Create", "Drop", "Alter", "Copy", "Command", "Attach", "Detach", "Pragma", "Set", "Transaction", "Merge", "Into", "Export", "LoadData", "Lock", "Grant", "Revoke", "Execute", "Use", "Unnest", "Lateral", "Pivot", "Unpivot", "TableSample"})


def validate_sql(sql: str, allowed_tables=VIEW_NAMES) -> str:
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 30000:
        raise SQLSafetyError("查询为空或超过长度限制")
    try:
        statements = sqlglot.parse(sql, read="duckdb", error_level="RAISE")
    except Exception as exc:
        raise SQLSafetyError("SQL无法解析") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise SQLSafetyError("只允许单条SELECT查询")
    tree = statements[0]
    nodes = list(tree.walk())
    if len(nodes) > 2500:
        raise SQLSafetyError("查询结构超过限制")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for node in nodes:
        if type(node).__name__ in FORBIDDEN_NODES:
            raise SQLSafetyError("查询包含禁止的操作")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise SQLSafetyError("不允许递归查询")
        if isinstance(node, exp.Table):
            if node.db or node.catalog or not isinstance(node.this, exp.Identifier):
                raise SQLSafetyError("不允许外部表、表函数或跨库访问")
            if node.name.lower() not in allowed_tables and node.name.lower() not in ctes:
                raise SQLSafetyError(f"未开放的数据表：{node.name}")
        if isinstance(node, exp.Func):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name().upper()
            if name not in ALLOWED_FUNCTIONS:
                raise SQLSafetyError(f"未开放的函数：{name}")
    if not list(tree.find_all(exp.Table)):
        raise SQLSafetyError("查询必须访问业务语义视图")
    return sql


# Alias for callers/tests that prefer explicit guard terminology.
assert_readonly = validate_sql
