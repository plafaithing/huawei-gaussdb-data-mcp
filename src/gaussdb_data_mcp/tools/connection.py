"""连接管理工具：连接健康检查与池状态（对应需求 is_database_connected /
get_database_connection_info）。

设计说明：本服务的连接由凭证驱动自动池化（ConnectionPool 常驻 server 内部），
无需显式 connect_to_database 工具——首次工具调用即按 credentials.json 建连并缓存。
单凭证单池：一个 MCP 实例绑定一个数据库（多数据源为多实例部署，见 README）。
"""

import asyncio
from typing import Any

from huaweicloud_mcp_core import ToolRisk, guard

from ..credentials import DbCredentials
from ..drivers import get_driver
from .sql import _pool, _settings


@guard(risk=ToolRisk.READ)
async def is_database_connected() -> dict[str, Any]:
    """Check whether a database connection can be established right now.

    Scenario: verify connectivity and credentials before running real queries.
    Executes SELECT 1 through the connection pool (proves the full path:
    credentials load, driver connect, session usable).

    Returns: connected (true/false), plus pool and driver context for diagnosis.
    """

    def _probe() -> dict[str, Any]:
        settings = _settings()
        driver = get_driver()
        creds = DbCredentials.load(settings)
        pool = _pool(settings)
        state: dict[str, Any] = {
            "connected": False,
            "driver": driver.driver_name(),
            "pool_free": pool.free_count(),
        }
        try:
            with pool.read_connection(creds, settings=settings) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            state["connected"] = True
        except Exception as exc:  # 连接失败是有效返回值而非工具异常
            state["error"] = type(exc).__name__
        return state

    return await asyncio.to_thread(_probe)


@guard(risk=ToolRisk.READ)
async def get_connection_info() -> dict[str, Any]:
    """Report cached connection-pool state and non-sensitive connection context.

    Scenario: inspect how many pooled connections are idle, which driver is in
    use, and whether a credential rotation is pending — the password never
    leaves the credentials file (only a fingerprint hash is tracked).
    """

    def _info() -> dict[str, Any]:
        settings = _settings()
        creds = DbCredentials.load(settings)
        pool = _pool(settings)
        return {
            "pool_free": pool.free_count(),
            "driver": get_driver().driver_name(),
            "host": creds.host,
            "port": creds.port,
            "dbname": creds.dbname,
            "user": creds.user,
            "sslmode": creds.sslmode,
            "credential_fingerprint": creds.fingerprint,
            "statement_timeout_ms": settings.statement_timeout_ms,
        }

    return await asyncio.to_thread(_info)
