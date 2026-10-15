"""SQL 执行工具：三通道 + explain。

通道锁：工具静态风险（core 中间件的只读拦截/confirm 门）+ 工具体内
classify_sql 强制分类一致。DDL 还需 GAUSSDB_DATA_MCP_ENABLE_DDL=true。
注册模式：本模块只定义 @guard 裸函数，由 server.build_sql_server 统一
add_tool 注册（MCPServer 实例是函数局部变量，勿用模块级装饰器）。
"""

import asyncio
import threading
from typing import Any

from huaweicloud_mcp_core import ToolRisk, guard
from huaweicloud_mcp_core.errors import McpToolError

from ..config import DataMcpSettings
from ..credentials import DbCredentials
from ..db import ConnectionPool, _cursor
from ..sql_guard import SqlClassification, classify_sql

_POOL: ConnectionPool | None = None
_POOL_LOCK = threading.Lock()


def _pool(settings: DataMcpSettings) -> ConnectionPool:
    """延迟初始化：池尺寸来自环境变量（GAUSSDB_DATA_MCP_POOL_MIN/MAX）。"""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ConnectionPool(pool_min=settings.pool_min, pool_max=settings.pool_max)
        return _POOL


def _settings() -> DataMcpSettings:
    # 每调用读 env：read_only 是启动快照（core 中间件构造时固化）；enable_ddl/资源上限是
    # 每调用热读（可运行期通过 env 变更并立即生效）；文件类（凭证/tokens）的热轮换在文件层。
    # per-call from_env() 主要是为了测试可 monkeypatch。
    return DataMcpSettings.from_env()


def _classify_for_channel(
    query: str, channel: ToolRisk, *, max_sql_chars: int
) -> SqlClassification:
    result = classify_sql(query, max_sql_chars=max_sql_chars)
    if result.risk is not channel:
        raise McpToolError(
            "GAUSSDB_DATA_MCP_RISK_MISMATCH",
            f"This statement is {result.risk.value} ({result.kind}); "
            f"it does not match the {channel.value} channel.",
            hint="Call the tool that matches the statement, or rewrite the statement.",
        )
    return result


def _format_rows(cursor: Any, settings: DataMcpSettings) -> dict[str, Any]:
    columns = [d[0] for d in cursor.description or []]
    fetched = cursor.fetchmany(settings.max_rows + 1)
    truncated = len(fetched) > settings.max_rows
    rows = [
        [_cell(value, settings.max_cell_chars) for value in row]
        for row in fetched[: settings.max_rows]
    ]
    return {"columns": columns, "row_count": len(rows), "rows": rows, "truncated": truncated}


def _cell(value: Any, limit: int) -> Any:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


_EXEC_ERROR_LIMIT = 300


def _exec_failed(exc: Exception) -> McpToolError:
    # 裸驱动异常（含池内死连接）统一映射：hint 兼顾语法错误与连接回收两种情况。
    # 原始报错文本对诊断有价值故保留（README 已知限制有声明），但截断到 300 字符：
    # 数据库报错可能含 SQL 片段/表结构，限界即可，不做正则脱敏（保持简单）。
    prefix = "Database execution failed: "
    detail = f"{type(exc).__name__}: {exc}"
    budget = _EXEC_ERROR_LIMIT - len(prefix)
    if len(detail) > budget:
        detail = detail[: budget - 1] + "…"
    return McpToolError(
        "GAUSSDB_DATA_MCP_EXEC_FAILED",
        prefix + detail,
        hint="Check the SQL syntax; if a pooled connection was reclaimed, retrying once is safe.",
    )


def _run_channel(query: str, channel: ToolRisk) -> dict[str, Any]:
    settings = _settings()
    result = _classify_for_channel(query, channel, max_sql_chars=settings.max_sql_chars)
    if channel is ToolRisk.DANGEROUS and not settings.enable_ddl:
        raise McpToolError(
            "GAUSSDB_DATA_MCP_DDL_DISABLED",
            "DDL execution is disabled.",
            hint="Set GAUSSDB_DATA_MCP_ENABLE_DDL=true (with GAUSSDB_DATA_MCP_READ_ONLY=false).",
        )
    creds = DbCredentials.load(settings)
    try:
        if channel is ToolRisk.READ:
            with _pool(settings).read_connection(creds, settings=settings) as conn:
                with _cursor(conn) as cur:
                    cur.execute(query)
                    if cur.description is None:
                        return {"columns": [], "row_count": 0, "rows": [], "truncated": False}
                    return _format_rows(cur, settings)
        affected = _pool(settings).run_write(creds, settings=settings, sql=query)
    except McpToolError:
        raise
    except Exception as exc:
        raise _exec_failed(exc) from exc
    return {"statement_kind": result.kind, "rows_affected": affected}


@guard(risk=ToolRisk.READ)
async def execute_sql(query: str) -> dict[str, Any]:
    """Execute one read-only SQL statement against GaussDB (SELECT / VALUES / SHOW /
    EXPLAIN / TABLE / WITH ... SELECT).

    Scenario: query business data after inspecting schemas with the metadata tools.
    Statements that modify data are rejected here with GAUSSDB_DATA_MCP_RISK_MISMATCH;
    use execute_sql_write or execute_sql_dangerous explicitly.

    Args:
        query: Exactly one SQL statement. Multi-statement text, PREPARE/EXECUTE, SET,
            COPY and other utility statements are rejected.
    """
    return await asyncio.to_thread(_run_channel, query, ToolRisk.READ)


@guard(risk=ToolRisk.WRITE)
async def execute_sql_write(query: str, confirm: bool = False) -> dict[str, Any]:
    """Execute one data-modifying statement (INSERT / UPDATE / DELETE / MERGE).

    Scenario: apply data changes the user explicitly asked for. Requires
    GAUSSDB_DATA_MCP_READ_ONLY=false at server start. Executes in autocommit mode;
    each call is one standalone statement.

    Args:
        query: Exactly one DML statement. DDL inside is rejected; use
            execute_sql_dangerous for DDL.
        confirm: Reserved for guardrail confirm rules; keep false.
    """
    return await asyncio.to_thread(_run_channel, query, ToolRisk.WRITE)


@guard(risk=ToolRisk.DANGEROUS)
async def execute_sql_dangerous(query: str, confirm: bool = False) -> dict[str, Any]:
    """Execute one DDL statement (CREATE / ALTER / DROP / TRUNCATE / GRANT ...).

    Scenario: schema changes requested explicitly by the user. Requires
    GAUSSDB_DATA_MCP_READ_ONLY=false and GAUSSDB_DATA_MCP_ENABLE_DDL=true. Review the
    statement carefully: DROP/TRUNCATE are irreversible.

    Args:
        query: Exactly one DDL statement.
        confirm: Set true on the second call after reviewing the confirmation envelope.
    """
    return await asyncio.to_thread(_run_channel, query, ToolRisk.DANGEROUS)


@guard(risk=ToolRisk.READ)
async def explain_sql(query: str) -> dict[str, Any]:
    """Run EXPLAIN on a SELECT statement without executing it.

    Scenario: check the query plan before running an expensive query.

    Args:
        query: One SELECT / TABLE / WITH ... SELECT statement.
    """
    return await asyncio.to_thread(_run_explain, query)


def _run_explain(query: str) -> dict[str, Any]:
    settings = _settings()
    result = classify_sql(query, max_sql_chars=settings.max_sql_chars)
    # 必须先按 risk 锁 READ 通道：WITH 首关键词的写 CTE（WITH x AS (INSERT ...) SELECT）
    # 的 kind 也是 "WITH"，仅按 kind 白名单判定会放行写语句进 explain。
    if result.risk is not ToolRisk.READ or result.kind not in {"SELECT", "WITH", "TABLE"}:
        raise McpToolError(
            "GAUSSDB_DATA_MCP_RISK_MISMATCH",
            f"explain_sql only accepts SELECT-class statements, got {result.kind}.",
        )
    creds = DbCredentials.load(settings)
    try:
        with _pool(settings).read_connection(creds, settings=settings) as conn:
            with _cursor(conn) as cur:
                cur.execute(f"EXPLAIN {query}")
                return _format_rows(cur, settings)
    except McpToolError:
        raise
    except Exception as exc:
        raise _exec_failed(exc) from exc
