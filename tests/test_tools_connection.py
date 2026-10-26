"""tools/connection.py 单元测试：mock 池与凭证，验证健康检查与池状态信封。"""

from types import SimpleNamespace

import pytest

from gaussdb_data_mcp.tools import connection as conn_tools
from gaussdb_data_mcp.tools import sql as sql_tools  # _pool() 实际读 sql 模块级 _POOL


@pytest.fixture
def fake_db_creds(monkeypatch):
    # 与 test_tools_sql.py 的同名 fixture 等价（本地复制，避免 tests 包化）
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", "")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_HOST", "10.0.0.1")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_PORT", "8000")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_DBNAME", "db")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_USER", "u")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_PASSWORD", "p")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DB_SSLMODE", "disable")


def _install_pool(free=2, raise_on_read=None):
    pool = SimpleNamespace(free_count=lambda: free)

    class _ReadCtx:
        def __enter__(self):
            if raise_on_read:
                raise raise_on_read
            return SimpleNamespace(cursor=lambda: _CursorHolder())

        def __exit__(self, *a):
            return False

    class _CursorHolder:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return (1,)

    pool.read_connection = lambda creds, settings: _ReadCtx()
    sql_tools._POOL = pool
    conn_tools._POOL = pool
    return pool


def _use_pool(pool):
    conn_tools._POOL = pool


@pytest.fixture
def pool_stub():
    pool = _install_pool()
    yield pool
    conn_tools._POOL = None


async def test_connected_happy_path(pool_stub, fake_db_creds):
    result = await conn_tools.is_database_connected()
    assert result["connected"] is True
    assert result["driver"] in ("psycopg2", "pyodbc")
    assert "error" not in result


async def test_connect_failure_returns_state_not_raise(pool_stub, fake_db_creds):
    pool = _install_pool(raise_on_read=RuntimeError("server closed"))
    sql_tools._POOL = pool
    conn_tools._POOL = pool
    try:
        result = await conn_tools.is_database_connected()
    finally:
        sql_tools._POOL = None
        conn_tools._POOL = None
    assert result["connected"] is False
    assert result["error"] == "RuntimeError"


async def test_connection_info_fields(pool_stub, fake_db_creds):
    result = await conn_tools.get_connection_info()
    assert result["pool_free"] == 2
    assert result["driver"] in ("psycopg2", "pyodbc")
    assert result["dbname"] == "db"
    assert "password" not in result
    assert len(result["credential_fingerprint"]) == 16


async def test_connection_info_pool_not_initialized(fake_db_creds):
    sql_tools._POOL = None
    conn_tools._POOL = None
    try:
        result = await conn_tools.get_connection_info()
        assert result["pool_free"] == 0
    finally:
        sql_tools._POOL = None
        conn_tools._POOL = None
