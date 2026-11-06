"""连接管理工具：连接健康检查与池状态（对应需求 is_database_connected /
get_database_connection_info）。

设计说明：本服务的连接由凭证驱动自动池化（ConnectionPool 常驻 server 内部），
无需显式 connect_to_database 工具——首次工具调用即按 credentials.json 建连并缓存。
单凭证单池：一个 MCP 实例绑定一个数据库（多数据源为多实例部署，见 README）。
"""

import asyncio
from typing import Any

from dataclasses import replace as _dc_replace

from huaweicloud_mcp_core import ToolRisk, guard
from huaweicloud_mcp_core.errors import McpToolError

from ..credentials import DbCredentials
from ..db import _cursor
from ..drivers import get_driver
from .sql import _pool, _settings

# GaussDB 系统库（das 轨 pg_database 实测全集）；用户库 = 全集剔除后的部分
_SYSTEM_DATABASES = {"postgres", "template0", "template1", "templatea", "templatem"}


@guard(risk=ToolRisk.READ)
async def is_database_connected(instance: str | None = None) -> dict[str, Any]:
    """Check whether a database connection can be established right now.

    Scenario: verify connectivity and credentials before running real queries.
    Executes SELECT 1 through the connection pool (proves the full path:
    credentials load, driver connect, session usable).

    Returns: connected (true/false), plus pool and driver context for diagnosis.
    """

    def _probe() -> dict[str, Any]:
        settings = _settings()
        driver = get_driver()
        creds = DbCredentials.load(settings, instance)
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
async def get_connection_info(instance: str | None = None) -> dict[str, Any]:
    """Report cached connection-pool state and non-sensitive connection context.

    Scenario: inspect how many pooled connections are idle, which driver is in
    use, and whether a credential rotation is pending — the password never
    leaves the credentials file (only a fingerprint hash is tracked).
    """

    def _info() -> dict[str, Any]:
        settings = _settings()
        creds = DbCredentials.load(settings, instance)
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


@guard(risk=ToolRisk.WRITE)
async def register_das_connection(instance_id: str, username: str, password: str,
                                  database_name: str | None = None) -> dict[str, Any]:
    """Register a DAS connection (RegisterDbUser, datastore_type=gaussdbv5) and persist
    the returned connection id into credentials.json (survey/07 §6/§8).

    Scenario: first-time setup per instance. Afterwards execute tools only need the
    instance id. The password is handed to DAS once; the execution path never uses it
    (it stays in the credentials file only if provided, for auto-register/self-heal).

    Args:
        instance_id: GaussDB instance ID.
        username: Database account (prefer a least-privilege account).
        password: Database password (single-time input; audit side masks it).
        database_name: Optional default database; postgres when omitted.
    """

    def _reg() -> dict[str, Any]:
        settings = _settings()
        if settings.driver != "das":
            raise McpToolError(
                "GAUSSDB_DATA_MCP_DRIVER_MISMATCH",
                "register_das_connection requires GAUSSDB_DATA_MCP_DRIVER=das.",
            )
        base = DbCredentials.load(settings, instance_id)  # 全局 ak/sk/region + 实例条目
        creds = _dc_replace(base, user=username, password=password)
        conn_id = _pool(settings).register_db_user(creds, instance_id)
        if settings.credentials_file:
            from ..credentials import save_das_connection
            save_das_connection(settings.credentials_file, instance_id, conn_id, database_name)
        return {
            "instance_id": instance_id,
            "das_connection_id": conn_id,
            "database_name": database_name or base.dbname,
            "persisted": bool(settings.credentials_file),
        }

    return await asyncio.to_thread(_reg)


@guard(risk=ToolRisk.READ)
async def list_user_databases(instance: str | None = None) -> dict[str, Any]:
    """List non-system databases to guide first-time database selection.

    Scenario: after the first connection (which lands on the pg catalog database),
    check whether a user database exists. If yes, the user picks one (set 'dbname'
    in the credentials entry, hot-reloaded); if none, guide the user to create one
    via CREATE DATABASE (the das track auto-applies panelKey for it, survey/07).
    System databases (postgres/templates) are excluded.
    """

    def _list() -> dict[str, Any]:
        settings = _settings()
        creds = DbCredentials.load(settings, instance)
        with _pool(settings).read_connection(creds, settings=settings) as conn:
            with _cursor(conn) as cur:
                cur.execute("SELECT datname, datcompatibility FROM pg_database ORDER BY datname")
                all_dbs = [(row[0], row[1]) for row in cur.fetchall()]
        user_dbs = [d for d in all_dbs if d[0] not in _SYSTEM_DATABASES]
        out: dict[str, Any] = {
            "current_database": creds.dbname,
            "system_databases": sorted(d[0] for d in all_dbs if d[0] in _SYSTEM_DATABASES),
            "user_databases": sorted(d[0] for d in user_dbs),
            "compatibility": {d[0]: d[1] for d in user_dbs},
        }
        if not user_dbs:
            out["guidance"] = (
                "No user database found. Create one first, e.g. "
                "execute_sql_dangerous('CREATE DATABASE mydb'), then set 'dbname' "
                "in the credentials entry (hot-reloaded) to switch."
            )
        return out

    return await asyncio.to_thread(_list)
