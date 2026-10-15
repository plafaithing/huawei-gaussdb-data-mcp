"""tools/sql.py 单元测试：mock ConnectionPool，专注通道锁/开关/格式化逻辑。

错误断言说明：core @guard 包装后 McpToolError 不再向上 raise，而是返回
{"error": {code, message, hint}}（core 的错误信封设计，真实链路同样以工具
结果返回），因此通道锁/开关测试断言 error 信封的错误码，语义与
pytest.raises(McpToolError) 等价，且覆盖的是注册入口的完整行为。
"""

from types import SimpleNamespace

import pytest

from gaussdb_data_mcp.tools import sql as sql_tools


class _Ctx:
    """通用 context manager：read_connection / cursor 两层 with 都用它。"""

    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self._value

    def __exit__(self, *a):
        return False


def _make_pool(rows=(("1", "a"), ("2", "b")), description=(("id",), ("name",))):
    # 签名对齐 db.ConnectionPool：read_connection/run_write 均为 keyword-only settings/sql；
    # cursor 直出并带 close（db._cursor 手动关闭——pyodbc cursor 无上下文管理器）
    cursor = SimpleNamespace(
        description=description,
        rowcount=len(rows),
        execute=lambda q: None,
        fetchmany=lambda n: [list(r) for r in rows][:n],
        close=lambda: None,
    )
    conn = SimpleNamespace(cursor=lambda: cursor)
    return SimpleNamespace(
        read_connection=lambda creds, settings: _Ctx(conn),
        run_write=lambda creds, settings, sql: 3,
    )


def _error_code(result):
    assert isinstance(result, dict), f"expected error envelope, got: {result!r}"
    assert "error" in result, f"expected error envelope, got: {result!r}"
    return result["error"]["code"]


@pytest.fixture
def pool_factory():
    def _install(rows=(("1", "a"), ("2", "b")), description=(("id",), ("name",))):
        pool = _make_pool(rows, description)
        sql_tools._POOL = pool
        return pool

    yield _install
    sql_tools._POOL = None


@pytest.fixture
def fake_db_creds(monkeypatch):
    # 走 db 路径的测试必须通过 DbCredentials.load 的必填校验；
    # 10.x 是私网地址，不触发「公网 + 弱 sslmode」的 fail-closed 拦截。
    for key, value in {
        "GAUSSDB_DATA_MCP_DB_HOST": "10.0.0.1",
        "GAUSSDB_DATA_MCP_DB_PORT": "8000",
        "GAUSSDB_DATA_MCP_DB_DBNAME": "db",
        "GAUSSDB_DATA_MCP_DB_USER": "u",
        "GAUSSDB_DATA_MCP_DB_PASSWORD": "p",
    }.items():
        monkeypatch.setenv(key, value)


async def test_execute_sql_read_happy_path(pool_factory, fake_db_creds):
    pool_factory()
    result = await sql_tools.execute_sql("SELECT id, name FROM t")
    assert result["columns"] == ["id", "name"]
    assert result["row_count"] == 2 and result["truncated"] is False


async def test_execute_sql_write_happy_path(pool_factory, fake_db_creds):
    pool_factory()
    result = await sql_tools.execute_sql_write("DELETE FROM t WHERE id = 1")
    assert result == {"statement_kind": "DELETE", "rows_affected": 3}


async def test_execute_sql_rejects_write_statement():
    assert _error_code(await sql_tools.execute_sql("DELETE FROM t")) == (
        "GAUSSDB_DATA_MCP_RISK_MISMATCH"
    )


async def test_execute_sql_write_rejects_read_statement():
    assert _error_code(await sql_tools.execute_sql_write("SELECT 1")) == (
        "GAUSSDB_DATA_MCP_RISK_MISMATCH"
    )


async def test_execute_sql_write_rejects_ddl_statement():
    assert _error_code(await sql_tools.execute_sql_write("DROP TABLE t")) == (
        "GAUSSDB_DATA_MCP_RISK_MISMATCH"
    )


async def test_execute_sql_dangerous_rejects_write_statement():
    # 通道锁第四向：DANGEROUS 通道拒 DML（防把 DROP 降级之外的反向偷渡）
    assert _error_code(await sql_tools.execute_sql_dangerous("DELETE FROM t")) == (
        "GAUSSDB_DATA_MCP_RISK_MISMATCH"
    )


async def test_dangerous_requires_enable_ddl(monkeypatch):
    monkeypatch.setenv("GAUSSDB_DATA_MCP_ENABLE_DDL", "false")
    assert _error_code(await sql_tools.execute_sql_dangerous("DROP TABLE t")) == (
        "GAUSSDB_DATA_MCP_DDL_DISABLED"
    )


async def test_row_truncation(monkeypatch, pool_factory, fake_db_creds):
    monkeypatch.setenv("GAUSSDB_DATA_MCP_MAX_ROWS", "1")
    pool_factory(rows=(("1", "a"), ("2", "b")))
    result = await sql_tools.execute_sql("SELECT id, name FROM t")
    assert result["row_count"] == 1 and result["truncated"] is True


async def test_cell_truncation(monkeypatch, pool_factory, fake_db_creds):
    monkeypatch.setenv("GAUSSDB_DATA_MCP_MAX_CELL_CHARS", "4")
    pool_factory(rows=(("1", "abcdefghijk"),), description=(("id",), ("v",)))
    result = await sql_tools.execute_sql("SELECT id, v FROM t")
    assert len(result["rows"][0][1]) <= 4 + len("…")


async def test_explain_wraps_select(pool_factory, fake_db_creds):
    pool_factory(rows=(("Seq Scan",),), description=(("QUERY PLAN",),))
    result = await sql_tools.explain_sql("SELECT * FROM t")
    assert result["columns"] == ["QUERY PLAN"]


async def test_explain_rejects_non_select():
    assert _error_code(await sql_tools.explain_sql("UPDATE t SET a = 1")) == (
        "GAUSSDB_DATA_MCP_RISK_MISMATCH"
    )


async def test_explain_rejects_write_cte(pool_factory, fake_db_creds):
    # WITH 首关键词的写 CTE：kind 是 "WITH"，但整体 risk 是 WRITE——
    # explain 通道锁必须按 risk 拒，仅按 kind 白名单会放行
    assert _error_code(await sql_tools.explain_sql(
        "WITH x AS (INSERT INTO t VALUES (1)) SELECT * FROM x"
    )) == "GAUSSDB_DATA_MCP_RISK_MISMATCH"


async def test_raw_db_exception_mapped(pool_factory, fake_db_creds):
    # 池内死连接借出 → SET READ ONLY 抛裸驱动异常 → 必须映射为错误信封（Task 5 审查 Minor 3）
    pool = pool_factory()

    class _Boom:
        def __enter__(self):
            raise RuntimeError("server closed the connection unexpectedly")

        def __exit__(self, *a):
            return False

    pool.read_connection = lambda creds, settings: _Boom()
    assert _error_code(await sql_tools.execute_sql("SELECT 1")) == "GAUSSDB_DATA_MCP_EXEC_FAILED"


async def test_exec_failed_message_truncated(pool_factory, fake_db_creds):
    # issue #1：数据库报错文本可能含 SQL 片段/表结构——透传给客户端有诊断价值，
    # 但截断到 300 字符限界（保留异常类型名）
    pool = pool_factory()

    class _Boom:
        def __enter__(self):
            raise RuntimeError("relation \"public.secret_table\" does not exist: " + "x" * 1000)

        def __exit__(self, *a):
            return False

    pool.read_connection = lambda creds, settings: _Boom()
    result = await sql_tools.execute_sql("SELECT 1")
    message = result["error"]["message"]
    assert len(message) <= 300
    assert "RuntimeError" in message  # 类型名保留


def test_pool_uses_env_sizes(monkeypatch):
    # 计划修正 2 的锁定：池尺寸吃环境变量
    monkeypatch.setenv("GAUSSDB_DATA_MCP_POOL_MIN", "2")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_POOL_MAX", "5")
    from gaussdb_data_mcp.config import DataMcpSettings

    settings = DataMcpSettings.from_env()
    pool = sql_tools._pool(settings)
    try:
        assert pool._min == 2 and pool._max == 5
    finally:
        sql_tools._POOL = None
