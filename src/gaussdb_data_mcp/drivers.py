"""数据库驱动抽象：psycopg2（Linux/官方 whl）与 pyodbc（Windows/GaussDB ODBC）双轨。

自动选择：
- 环境变量 GAUSSDB_DATA_MCP_DRIVER=psycopg2|pyodbc 强制指定
- 未指定时按平台自动：Windows → pyodbc，其他 → psycopg2
统一接口（与 db.py 的调用约定一致）：
- connect(creds, settings) -> 连接对象（autocommit=True 已设置）
- placeholder() -> "%s" 或 "?"
- driver_name() -> "psycopg2" | "pyodbc"

实测背书（ops 云实例验证，2026-10）：ODBC 驱动注册名 "GaussDB Unicode"、
sha256 认证 / SSL require / 只读会话 / "?" 占位符 / 运行期 conn.close 全部通过；
进程退出阶段 GaussDB ODBC DLL 堆损坏（0xC0000374）由 server.py 的退出规避处理。
"""

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from huaweicloud_mcp_core.errors import McpToolError

from .config import DataMcpSettings
from .credentials import DbCredentials

_DRIVER_ENV = "GAUSSDB_DATA_MCP_DRIVER"
_ODBC_DRIVER_ENV = "GAUSSDB_DATA_MCP_ODBC_DRIVER"
_DEFAULT_ODBC_DRIVER = "GaussDB Unicode"


def _auth_failed() -> McpToolError:
    # 与锁号护栏配套：密码连错 10 次锁号 1 天，认证失败绝不重试
    return McpToolError(
        "GAUSSDB_DATA_MCP_AUTH_FAILED",
        "Database authentication failed. Retrying is disabled on purpose: "
        "10 failed logins lock the account for one day.",
        hint="Fix credentials.json; the server keeps running without retry.",
    )


@contextmanager
def _cursor(conn: Any) -> Iterator[Any]:
    """统一游标作用域：psycopg2 cursor 支持 with 而 pyodbc cursor 不支持，
    但两者都有 close()——统一手动关闭。"""
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


class Psycopg2Driver:
    """Linux/生产主路径：GaussDB 自研 psycopg2（仅 Linux/py311，懒加载）。"""

    def driver_name(self) -> str:
        return "psycopg2"

    def placeholder(self) -> str:
        return "%s"

    def _module(self) -> Any:
        try:
            import psycopg2  # GaussDB 自研 psycopg2（懒加载；已导入时仅 sys.modules 查找）
        except ImportError as exc:
            raise McpToolError(
                "GAUSSDB_DATA_MCP_DRIVER_MISSING",
                "GaussDB psycopg2 driver is not installed.",
                hint="Install the official GaussDB_Kernel_*_py311_linux whl "
                     "(Linux + Python 3.11 only, see docs).",
            ) from exc
        return psycopg2

    def connect(self, creds: DbCredentials, settings: DataMcpSettings) -> Any:
        psycopg2 = self._module()
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
                raise _auth_failed() from exc
            raise McpToolError(
                "GAUSSDB_DATA_MCP_CONNECT_FAILED", f"Database connection failed: {text}"
            ) from exc
        conn.autocommit = True
        return conn


class PyodbcDriver:
    """Windows 原生路径：pyodbc + GaussDB ODBC（gsqlodbc 安装器注册 GaussDB Unicode）。"""

    def driver_name(self) -> str:
        return "pyodbc"

    def placeholder(self) -> str:
        return "?"

    def _module(self) -> Any:
        try:
            import pyodbc  # 懒加载：pyodbc 不进主依赖（uv sync --group windows）
        except ImportError as exc:
            raise McpToolError(
                "GAUSSDB_DATA_MCP_DRIVER_MISSING",
                "pyodbc is not installed.",
                hint="uv pip install pyodbc (or: uv sync --group windows) and install "
                     "the GaussDB ODBC driver; see README section 2.5.",
            ) from exc
        return pyodbc

    def connect(self, creds: DbCredentials, settings: DataMcpSettings) -> Any:
        pyodbc = self._module()
        odbc_driver = os.environ.get(_ODBC_DRIVER_ENV, "").strip() or _DEFAULT_ODBC_DRIVER
        conn_str = ";".join((
            f"Driver={{{odbc_driver}}}",
            f"Server={creds.host}",
            f"Port={creds.port}",
            f"Database={creds.dbname}",
            f"Uid={creds.user}",
            f"Pwd={creds.password}",
            f"Sslmode={creds.sslmode}",
        ))
        try:
            conn = pyodbc.connect(conn_str, timeout=10)
        except pyodbc.Error as exc:
            if _is_auth_failure(pyodbc, exc):
                raise _auth_failed() from exc
            raise McpToolError(
                "GAUSSDB_DATA_MCP_CONNECT_FAILED", f"Database connection failed: {exc}"
            ) from exc
        # 实测必须 autocommit：事务内 SET SESSION CHARACTERISTICS 不生效
        conn.autocommit = True
        # ODBC 无 options 启动参数：statement_timeout 连接后 SET（会话级，实测生效）
        with _cursor(conn) as cur:
            cur.execute(f"SET statement_timeout = {settings.statement_timeout_ms}")
        return conn


def _is_auth_failure(pyodbc: Any, exc: Exception) -> bool:
    # 认证失败判定：错误文本含 "password authentication failed"（psycopg2 轨
    # 的 pgcode 28P01 对应 ODBC 侧的该文本）；部分版本以登录超时类错误呈现
    # （设计口径 pyodbc.LoginTimeoutError——5.x 实无此属性，防御式探测，
    # 驱动定义了才按类型判定，避免 AttributeError）。
    login_timeout = getattr(pyodbc, "LoginTimeoutError", None)
    if login_timeout is not None and isinstance(exc, login_timeout):
        return True
    return "password authentication failed" in str(exc).lower()


def get_driver() -> Psycopg2Driver | PyodbcDriver:
    """驱动工厂：env 强制指定，否则按平台（Windows → pyodbc，其余 → psycopg2）。"""
    name = os.environ.get(_DRIVER_ENV, "").strip().lower()
    if not name:
        name = "pyodbc" if sys.platform == "win32" else "psycopg2"
    if name == "psycopg2":
        return Psycopg2Driver()
    if name == "pyodbc":
        return PyodbcDriver()
    raise McpToolError(
        "GAUSSDB_DATA_MCP_DRIVER_INVALID",
        f"{_DRIVER_ENV} must be 'psycopg2' or 'pyodbc' (got {name!r}).",
    )
