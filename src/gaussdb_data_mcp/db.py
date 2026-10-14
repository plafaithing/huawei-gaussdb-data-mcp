"""连接池：读连接复用（RESET ALL 归还），写/DDL 连接用完即关。

psycopg2 懒加载——GaussDB 自研驱动仅 Linux/py311（survey/03），
Windows 开发机上仅能通过 mock 测试本模块。
认证失败映射专用错误码且绝不重试：密码连错 10 次锁号 1 天（survey/03 §7.7）。
"""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from huaweicloud_mcp_core.errors import McpToolError

from .config import DataMcpSettings
from .credentials import DbCredentials

_IDLE_TTL_SECONDS = 600  # 服务端 session_timeout 默认 1800s，池内空闲必须更短


class ConnectionPool:
    def __init__(self, *, pool_min: int, pool_max: int) -> None:
        self._min, self._max = pool_min, pool_max
        self._lock = threading.Lock()
        self._free: list[tuple[float, Any]] = []  # (idle_since, connection)
        self._fingerprint: str | None = None
        self._psycopg2: Any = None

    # ---- 对外接口 ----

    @contextmanager
    def read_connection(
        self, creds: DbCredentials, *, settings: DataMcpSettings
    ) -> Iterator[Any]:
        conn = self._checkout(creds, settings)
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            yield conn
        finally:
            self._return(conn, fingerprint=creds.fingerprint)

    def run_write(self, creds: DbCredentials, *, settings: DataMcpSettings, sql: str) -> int:
        """写/DDL 通道：专用连接，autocommit，用完即关。返回受影响行数。"""
        conn = self._connect(creds, settings)
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.rowcount
        finally:
            conn.close()

    def free_count(self) -> int:
        with self._lock:
            return len(self._free)

    # ---- 内部 ----

    def _driver(self) -> Any:
        if self._psycopg2 is None:
            try:
                import psycopg2  # GaussDB 自研 psycopg2（懒加载）
            except ImportError as exc:
                raise McpToolError(
                    "GAUSSDB_DATA_MCP_DRIVER_MISSING",
                    "GaussDB psycopg2 driver is not installed.",
                    hint="Install the official GaussDB_Kernel_*_py311_linux whl "
                         "(Linux + Python 3.11 only, see docs).",
                ) from exc
            self._psycopg2 = psycopg2
        return self._psycopg2

    def _connect(self, creds: DbCredentials, settings: DataMcpSettings) -> Any:
        psycopg2 = self._driver()
        kwargs = dict(creds.connect_kwargs)
        kwargs.update(
            connect_timeout=10,
            application_name=settings.application_name,
            options=f"-c statement_timeout={settings.statement_timeout_ms}",
        )
        try:
            conn = psycopg2.connect(**kwargs)
        except psycopg2.OperationalError as exc:
            text = str(exc)
            if "password" in text.lower() or getattr(exc, "pgcode", None) == "28P01":
                raise McpToolError(
                    "GAUSSDB_DATA_MCP_AUTH_FAILED",
                    "Database authentication failed. Retrying is disabled on purpose: "
                    "10 failed logins lock the account for one day.",
                    hint="Fix credentials.json; the server keeps running without retry.",
                ) from exc
            raise McpToolError(
                "GAUSSDB_DATA_MCP_CONNECT_FAILED", f"Database connection failed: {text}"
            ) from exc
        conn.autocommit = True
        return conn

    def _checkout(self, creds: DbCredentials, settings: DataMcpSettings) -> Any:
        with self._lock:
            if self._fingerprint != creds.fingerprint:
                for _, conn in self._free:
                    try:
                        conn.close()
                    except Exception:  # 关闭失败不影响借出路径
                        pass
                self._free.clear()
                self._fingerprint = creds.fingerprint
            while self._free:
                idle_since, conn = self._free.pop()
                if time.monotonic() - idle_since <= _IDLE_TTL_SECONDS:
                    return conn
                try:
                    conn.close()
                except Exception:  # 过期连接关闭失败同样不阻塞
                    pass
        return self._connect(creds, settings)

    def _return(self, conn: Any, *, fingerprint: str) -> None:
        try:
            with conn.cursor() as cur:
                cur.execute("RESET ALL")
                # 会话级咨询锁不随 RESET ALL 释放：池化连接会替上个请求长期持锁，
                # 归还前显式解锁（仍在同一 try 内——解锁失败走下方 close 路径）。
                cur.execute("SELECT pg_advisory_unlock_all()")
            with self._lock:
                # 指纹必须与当前池一致：借出期间发生轮换的连接绝不能入池，
                # 否则下个请求会静默拿到旧实例/旧用户的连接。
                if fingerprint == self._fingerprint and len(self._free) < self._max:
                    self._free.append((time.monotonic(), conn))
                    return
        except Exception:  # 归还路径任何失败都退化为关闭连接
            pass
        try:
            conn.close()
        except Exception:  # 已尽力关闭，吞掉二次失败
            pass
