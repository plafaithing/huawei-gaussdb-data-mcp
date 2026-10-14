"""db.py 单元测试：全部 mock psycopg2，不触碰真实驱动（官方驱动仅 Linux/py311）。"""

import sys
from unittest.mock import MagicMock, patch

import pytest
from huaweicloud_mcp_core.errors import McpToolError

from gaussdb_data_mcp.config import DataMcpSettings
from gaussdb_data_mcp.credentials import DbCredentials
from gaussdb_data_mcp.db import ConnectionPool

CREDS = DbCredentials(host="h", port=8000, dbname="db", user="u", password="p",
                      fingerprint="fp1")
SETTINGS = DataMcpSettings()


@pytest.fixture(autouse=True)
def _psycopg2_track(monkeypatch):
    # 池测试锁定 psycopg2 轨（drivers.py 双轨；Windows 默认会是 pyodbc），
    # 保证生产主路径行为不变。pyodbc 轨见 test_drivers.py。
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DRIVER", "psycopg2")


def _fake_psycopg2(connect_error=None):
    # mod.OperationalError 必须是真异常类：_connect 的 except 子句要拿它匹配，
    # 且 side_effect 只有挂真异常实例才会 raise（挂 MagicMock 只会被调用并返回）。
    op_error_cls = type(
        "OperationalError", (Exception,),
        {"__str__": lambda s: "FATAL: invalid username/password"},
    )
    conn = MagicMock(name="conn")
    mod = MagicMock(name="psycopg2")
    mod.OperationalError = op_error_cls
    if connect_error:
        exc = op_error_cls("FATAL: invalid username/password")
        exc.pgcode = "28P01"
        mod.connect = MagicMock(side_effect=exc)
    else:
        mod.connect = MagicMock(return_value=conn)
    return mod, conn


def _executed(cursor):
    return [call.args[0] for call in cursor.execute.call_args_list]


def test_read_connection_sets_read_only_session():
    mod, conn = _fake_psycopg2()
    pool = ConnectionPool(pool_min=1, pool_max=4)
    with patch.dict(sys.modules, {"psycopg2": mod}):
        with pool.read_connection(CREDS, settings=SETTINGS):
            cursor = conn.cursor.return_value
            assert any("READ ONLY" in s for s in _executed(cursor))
        assert any("RESET ALL" in s for s in _executed(cursor))


def test_return_unlocks_session_advisory_locks():
    # 会话级咨询锁不随 RESET ALL 释放（终审发现 3）：池化连接可能替上个
    # 请求长期持锁阻塞他人——归还序列必须在 RESET ALL 之后显式 unlock_all
    mod, conn = _fake_psycopg2()
    pool = ConnectionPool(pool_min=1, pool_max=4)
    with patch.dict(sys.modules, {"psycopg2": mod}):
        with pool.read_connection(CREDS, settings=SETTINGS):
            pass
        cursor = conn.cursor.return_value
        executed = _executed(cursor)
        assert "SELECT pg_advisory_unlock_all()" in executed
        assert executed.index("RESET ALL") < executed.index("SELECT pg_advisory_unlock_all()")


def test_write_connection_is_closed_not_pooled():
    mod, conn = _fake_psycopg2()
    pool = ConnectionPool(pool_min=1, pool_max=4)
    with patch.dict(sys.modules, {"psycopg2": mod}):
        pool.run_write(CREDS, settings=SETTINGS, sql="INSERT INTO t VALUES (1)")
        conn.close.assert_called_once()          # 写连接用完即关
        assert pool.free_count() == 0             # 不进池


def test_auth_failure_no_retry():
    mod, _ = _fake_psycopg2(connect_error=True)
    pool = ConnectionPool(pool_min=1, pool_max=4)
    with patch.dict(sys.modules, {"psycopg2": mod}):
        with pytest.raises(McpToolError) as exc:
            with pool.read_connection(CREDS, settings=SETTINGS):
                pass
    assert "AUTH" in exc.value.code or "auth" in exc.value.message.lower()
    assert mod.connect.call_count == 1            # 防锁号：认证失败绝不重试


def test_credential_rotation_drains_pool():
    mod, conn = _fake_psycopg2()
    pool = ConnectionPool(pool_min=1, pool_max=4)
    with patch.dict(sys.modules, {"psycopg2": mod}):
        with pool.read_connection(CREDS, settings=SETTINGS):
            pass
        assert pool.free_count() == 1
        new_creds = DbCredentials(host="h2", port=8000, dbname="db", user="u",
                                  password="p", fingerprint="fp2")
        with pool.read_connection(new_creds, settings=SETTINGS):
            pass
        conn.close.assert_called()                # 旧指纹连接被关闭


def test_connect_params_include_timeout_and_app_name():
    mod, _ = _fake_psycopg2()
    pool = ConnectionPool(pool_min=1, pool_max=4)
    with patch.dict(sys.modules, {"psycopg2": mod}):
        with pool.read_connection(CREDS, settings=SETTINGS):
            pass
        kwargs = mod.connect.call_args.kwargs
        assert kwargs["connect_timeout"] == 10
        assert kwargs["application_name"] == "gaussdb_data_mcp"
        assert "statement_timeout=30000" in kwargs["options"]


def test_return_after_rotation_not_pooled():
    # 竞态锁定：fp1 借出未归还期间池轮换到 fp2；fp1 归还时必须 close 而非入池，
    # 否则下一个 fp2 请求会静默拿到旧实例/旧用户的连接。
    conn1, conn2 = MagicMock(name="conn_fp1"), MagicMock(name="conn_fp2")
    mod, _ = _fake_psycopg2()
    mod.connect = MagicMock(side_effect=[conn1, conn2])
    fp2 = DbCredentials(host="h2", port=8000, dbname="db", user="u",
                        password="p", fingerprint="fp2")
    pool = ConnectionPool(pool_min=1, pool_max=4)
    with patch.dict(sys.modules, {"psycopg2": mod}):
        with pool.read_connection(CREDS, settings=SETTINGS):
            with pool.read_connection(fp2, settings=SETTINGS):
                pass                      # fp2 checkout 触发轮换；fp2 归还入池
        # fp1 此时才归还：池指纹已是 fp2 → 必须 close，不得入池
        assert conn1.close.called
        assert pool.free_count() == 1     # 池里只剩 fp2 那条
        with pool.read_connection(fp2, settings=SETTINGS) as c3:
            assert c3 is conn2            # fp2 复用 conn2，绝不发到旧实例
        assert mod.connect.call_count == 2


def test_driver_missing_error():
    pool = ConnectionPool(pool_min=1, pool_max=4)
    with patch.dict(sys.modules, {"psycopg2": None}):
        with pytest.raises(McpToolError) as exc:
            pool.run_write(CREDS, settings=SETTINGS, sql="SELECT 1")
    assert "DRIVER" in exc.value.code or "driver" in exc.value.message.lower()
