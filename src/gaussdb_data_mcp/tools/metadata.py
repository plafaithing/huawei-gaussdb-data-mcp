"""元数据工具：schema / 表 / 字段浏览（全部 READ，参数化查询，标识符白名单校验）。

注册模式与 tools/sql.py 一致：@guard 裸函数，由 server.build_sql_server 统一
add_tool 注册。复用 sql.py 的延迟初始化池与行格式化（同一池、同一配置读取）。
标识符虽经参数化传入，仍先过白名单正则：information_schema 查询本身安全，
但严格校验让非法输入在触库前即被拒（fail-early，也便于调用方自查拼写）。
"""

import asyncio
import re
from typing import Any

from huaweicloud_mcp_core import ToolRisk, guard
from huaweicloud_mcp_core.errors import McpToolError

from ..credentials import DbCredentials
from ..db import _cursor
from ..drivers import get_driver
from .sql import _exec_failed, _format_rows, _pool, _settings

# GaussDB 对象名允许 $（A 兼容模式，survey/03），故白名单含之；
# 尾锚用 \Z 而非 $：$ 会放过末尾换行（"sch\n" 实测可通过）
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*\Z")


def _validate_identifier(value: str, name: str) -> str:
    if not _IDENTIFIER.match(value or ""):
        raise McpToolError(
            "GAUSSDB_DATA_MCP_INVALID_IDENTIFIER",
            f"{name} must match [A-Za-z_][A-Za-z0-9_$]*.",
        )
    return value


def _query(sql: str, params: tuple) -> dict[str, Any]:
    settings = _settings()
    creds = DbCredentials.load(settings)
    try:
        with _pool(settings).read_connection(creds, settings=settings) as conn:
            with _cursor(conn) as cur:
                cur.execute(sql, params)
                return _format_rows(cur, settings)
    except McpToolError:
        raise
    except Exception as exc:
        raise _exec_failed(exc) from exc


@guard(risk=ToolRisk.READ)
async def list_schemas() -> dict[str, Any]:
    """List database schemas visible to the MCP account.

    Scenario: discover the schema layout before picking tables.
    """
    return await asyncio.to_thread(
        _query, "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name", ()
    )


@guard(risk=ToolRisk.READ)
async def list_tables(schema: str = "public") -> dict[str, Any]:
    """List tables and views in a schema.

    Scenario: find candidate tables for a query.

    Args:
        schema: Schema name, default public.
    """
    _validate_identifier(schema, "schema")
    ph = get_driver().placeholder()  # psycopg2 "%s" / pyodbc "?"，对调用方透明
    return await asyncio.to_thread(
        _query,
        f"SELECT table_name, table_type FROM information_schema.tables "
        f"WHERE table_schema = {ph} ORDER BY table_name",
        (schema,),
    )


@guard(risk=ToolRisk.READ)
async def describe_table(schema: str, table: str) -> dict[str, Any]:
    """Describe columns of a table: name, type, nullability, default.

    Scenario: understand column semantics before writing SQL.

    Args:
        schema: Schema name.
        table: Table name.
    """
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    ph = get_driver().placeholder()
    return await asyncio.to_thread(
        _query,
        f"SELECT column_name, data_type, is_nullable, column_default "
        f"FROM information_schema.columns "
        f"WHERE table_schema = {ph} AND table_name = {ph} ORDER BY ordinal_position",
        (schema, table),
    )
