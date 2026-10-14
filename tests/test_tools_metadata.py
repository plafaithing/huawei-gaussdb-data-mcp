"""tools/metadata.py 单元测试：mock 池层，专注标识符校验与参数化查询。

错误断言说明与 test_tools_sql.py 一致：@guard 包装后 McpToolError 不再向上
raise，而是返回 {"error": {code, message, hint}} 信封（core 的错误信封设计，
真实链路同样以工具结果返回），因此标识符拒绝测试断言 error 信封的错误码，
语义与 pytest.raises(McpToolError) 等价，且覆盖注册入口的完整行为。
"""

from types import SimpleNamespace

import pytest

from gaussdb_data_mcp.tools import metadata as meta
from gaussdb_data_mcp.tools import sql as sql_tools


class _Ctx:
    """通用 context manager：read_connection / cursor 两层 with 都用它。"""

    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self._value

    def __exit__(self, *a):
        return False


def _error_code(result):
    assert isinstance(result, dict), f"expected error envelope, got: {result!r}"
    assert "error" in result, f"expected error envelope, got: {result!r}"
    return result["error"]["code"]


@pytest.fixture
def pool_factory():
    """安装 mock 池（捕获 execute(sql, params)），teardown 复位 _POOL。"""
    calls = []

    def _install(rows, description):
        cursor = SimpleNamespace(
            description=description,
            execute=lambda sql, params: calls.append((sql, params)),
            fetchmany=lambda n: [list(r) for r in rows][:n],
        )
        conn = SimpleNamespace(cursor=lambda: _Ctx(cursor))
        sql_tools._POOL = SimpleNamespace(
            read_connection=lambda creds, settings: _Ctx(conn)
        )
        return calls

    yield _install
    sql_tools._POOL = None


@pytest.fixture
def fake_db_creds(monkeypatch):
    # CREDENTIALS_FILE 置空：无凭证文件时 env 兜底（隔离本机文件配置）；
    # 10.x 是私网地址，不触发「公网 + 弱 sslmode」的 fail-closed 拦截。
    monkeypatch.setenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE", "")
    for key, value in {
        "GAUSSDB_DATA_MCP_DB_HOST": "10.0.0.1",
        "GAUSSDB_DATA_MCP_DB_PORT": "8000",
        "GAUSSDB_DATA_MCP_DB_DBNAME": "db",
        "GAUSSDB_DATA_MCP_DB_USER": "u",
        "GAUSSDB_DATA_MCP_DB_PASSWORD": "p",
    }.items():
        monkeypatch.setenv(key, value)


async def test_list_schemas(pool_factory, fake_db_creds):
    pool_factory([("public",), ("sales",)], (("schema_name",),))
    result = await meta.list_schemas()
    assert result["columns"] == ["schema_name"]
    assert result["rows"] == [["public"], ["sales"]]


async def test_list_tables_parameterized(pool_factory, fake_db_creds):
    calls = pool_factory(
        [("orders", "BASE TABLE")], (("table_name",), ("table_type",))
    )
    result = await meta.list_tables(schema="public")
    assert result["rows"] == [["orders", "BASE TABLE"]]
    # execute 第二参是参数元组，无字符串拼接
    assert calls[0][1] == ("public",)
    assert "table_schema = %s" in calls[0][0]


async def test_describe_table_parameterized(pool_factory, fake_db_creds):
    calls = pool_factory(
        [("id", "integer", "NO", None)],
        (("column_name",), ("data_type",), ("is_nullable",), ("column_default",)),
    )
    result = await meta.describe_table(schema="public", table="orders")
    assert result["rows"] == [["id", "integer", "NO", None]]
    assert calls[0][1] == ("public", "orders")
    assert "table_schema = %s AND table_name = %s" in calls[0][0]


async def test_raw_db_exception_mapped(pool_factory, fake_db_creds):
    # 池内死连接 → read_connection enter 即抛裸驱动异常 → 必须映射为 EXEC_FAILED
    # 信封（与 test_tools_sql.py 的 test_raw_db_exception_mapped 同一模式）
    pool_factory([("public",)], (("schema_name",),))

    class _Boom:
        def __enter__(self):
            raise RuntimeError("server closed the connection unexpectedly")

        def __exit__(self, *a):
            return False

    sql_tools._POOL.read_connection = lambda creds, settings: _Boom()
    assert _error_code(await meta.list_schemas()) == "GAUSSDB_DATA_MCP_EXEC_FAILED"


@pytest.mark.parametrize("bad", [
    "bad name; DROP TABLE t",
    "sch-ema",
    "",
    "1abc",
    "sch\n",
])
async def test_rejects_bad_identifier(bad):
    # 五类非法标识符（注入尝试/连字符/空串/首数字/尾部换行）对三个工具的参数位全拒；
    # 尾部换行条目锁定尾锚 \Z：$ 会放过 "sch\n"；
    # 校验在进入池层之前，未装 mock 池也能返回错误信封而非误建真池
    assert _error_code(await meta.list_tables(schema=bad)) == (
        "GAUSSDB_DATA_MCP_INVALID_IDENTIFIER"
    )
    assert _error_code(await meta.describe_table(schema=bad, table="orders")) == (
        "GAUSSDB_DATA_MCP_INVALID_IDENTIFIER"
    )
    assert _error_code(await meta.describe_table(schema="public", table=bad)) == (
        "GAUSSDB_DATA_MCP_INVALID_IDENTIFIER"
    )


async def test_validation_precedes_db_access(pool_factory):
    # 校验先于取连接：池已安装但坏标识符不触发任何 execute
    calls = pool_factory([], ())
    assert _error_code(await meta.list_tables(schema="sch-ema")) == (
        "GAUSSDB_DATA_MCP_INVALID_IDENTIFIER"
    )
    assert calls == []
