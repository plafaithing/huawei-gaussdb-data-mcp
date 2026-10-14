"""drivers.py 单元测试：驱动选择 / pyodbc 连接行为 / 退出规避。

全部 mock 驱动模块（psycopg2 官方驱动仅 Linux/py311；pyodbc 属 windows 可选
依赖组）——fake 模块的异常类必须是真类：驱动层 except 子句要按类匹配。
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from huaweicloud_mcp_core.errors import McpToolError

from gaussdb_data_mcp import server as server_mod
from gaussdb_data_mcp.config import DataMcpSettings
from gaussdb_data_mcp.credentials import DbCredentials
from gaussdb_data_mcp.drivers import Psycopg2Driver, PyodbcDriver, get_driver

CREDS = DbCredentials(host="h", port=8000, dbname="db", user="u", password="p",
                      sslmode="require", fingerprint="fp1")
SETTINGS = DataMcpSettings()


@pytest.fixture(autouse=True)
def _clean_driver_env(monkeypatch):
    # 隔离本机环境：选择逻辑不受外部 GAUSSDB_DATA_MCP_DRIVER 等变量影响
    monkeypatch.delenv("GAUSSDB_DATA_MCP_DRIVER", raising=False)
    monkeypatch.delenv("GAUSSDB_DATA_MCP_ODBC_DRIVER", raising=False)


# ---- 工厂：平台自动选择与强制指定 ----

def test_auto_select_windows_prefers_pyodbc(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(get_driver(), PyodbcDriver)


def test_auto_select_other_platforms_prefer_psycopg2(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(get_driver(), Psycopg2Driver)


def test_env_overrides_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DRIVER", "psycopg2")
    assert isinstance(get_driver(), Psycopg2Driver)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DRIVER", "pyodbc")
    assert isinstance(get_driver(), PyodbcDriver)


def test_invalid_driver_env_rejected(monkeypatch):
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DRIVER", "mysql")
    with pytest.raises(McpToolError) as exc:
        get_driver()
    assert exc.value.code == "GAUSSDB_DATA_MCP_DRIVER_INVALID"


# ---- 占位符 ----

def test_placeholders():
    assert Psycopg2Driver().placeholder() == "%s"
    assert Psycopg2Driver().driver_name() == "psycopg2"
    assert PyodbcDriver().placeholder() == "?"
    assert PyodbcDriver().driver_name() == "pyodbc"


# ---- pyodbc 连接行为（mock pyodbc 模块）----

def _fake_pyodbc():
    """fake pyodbc：异常类必须是真类（except pyodbc.Error 按类匹配）。"""
    error_cls = type("Error", (Exception,), {})
    mod = MagicMock(name="pyodbc")
    mod.Error = error_cls
    mod.InterfaceError = type("InterfaceError", (error_cls,), {})
    mod.LoginTimeoutError = type("LoginTimeoutError", (error_cls,), {})
    conn = MagicMock(name="conn")
    mod.connect = MagicMock(return_value=conn)
    return mod, conn


def _executed(cursor):
    return [call.args[0] for call in cursor.execute.call_args_list]


def test_pyodbc_connect_string_and_autocommit():
    mod, conn = _fake_pyodbc()
    with patch.dict(sys.modules, {"pyodbc": mod}):
        out = PyodbcDriver().connect(CREDS, SETTINGS)
    assert out is conn
    conn_str = mod.connect.call_args.args[0]
    for part in ("Driver={GaussDB Unicode}", "Server=h", "Port=8000", "Database=db",
                 "Uid=u", "Pwd=p", "Sslmode=require"):
        assert part in conn_str
    assert mod.connect.call_args.kwargs == {"timeout": 10}  # timeout 即连接超时
    assert conn.autocommit is True  # 实测必须：事务内 SET SESSION 不生效


def test_pyodbc_sets_session_statement_timeout():
    # ODBC 无 options 启动参数：statement_timeout 连接后 SET（会话级，实测生效）
    mod, conn = _fake_pyodbc()
    with patch.dict(sys.modules, {"pyodbc": mod}):
        PyodbcDriver().connect(CREDS, SETTINGS)
    cursor = conn.cursor.return_value
    assert "SET statement_timeout = 30000" in _executed(cursor)
    cursor.close.assert_called_once()  # pyodbc cursor 无 with，_cursor 手动 close


def test_pyodbc_driver_name_env_override(monkeypatch):
    monkeypatch.setenv("GAUSSDB_DATA_MCP_ODBC_DRIVER", "GaussDB ANSI")
    mod, _ = _fake_pyodbc()
    with patch.dict(sys.modules, {"pyodbc": mod}):
        PyodbcDriver().connect(CREDS, SETTINGS)
    assert "Driver={GaussDB ANSI}" in mod.connect.call_args.args[0]


def test_pyodbc_driver_missing():
    with patch.dict(sys.modules, {"pyodbc": None}):
        with pytest.raises(McpToolError) as exc:
            PyodbcDriver().connect(CREDS, SETTINGS)
    assert exc.value.code == "GAUSSDB_DATA_MCP_DRIVER_MISSING"
    assert "pyodbc" in exc.value.hint


def test_pyodbc_auth_failed_by_password_text():
    mod, _ = _fake_pyodbc()
    mod.connect = MagicMock(side_effect=mod.InterfaceError(
        '28000: password authentication failed for user "u"'))
    with patch.dict(sys.modules, {"pyodbc": mod}):
        with pytest.raises(McpToolError) as exc:
            PyodbcDriver().connect(CREDS, SETTINGS)
    assert exc.value.code == "GAUSSDB_DATA_MCP_AUTH_FAILED"


def test_pyodbc_auth_failed_by_login_timeout():
    # 设计口径：部分版本认证失败以登录超时类错误呈现（LoginTimeoutError）
    mod, _ = _fake_pyodbc()
    mod.connect = MagicMock(side_effect=mod.LoginTimeoutError("HYT00: login timeout"))
    with patch.dict(sys.modules, {"pyodbc": mod}):
        with pytest.raises(McpToolError) as exc:
            PyodbcDriver().connect(CREDS, SETTINGS)
    assert exc.value.code == "GAUSSDB_DATA_MCP_AUTH_FAILED"


def test_pyodbc_other_connect_error_mapped():
    mod, _ = _fake_pyodbc()
    mod.connect = MagicMock(side_effect=mod.Error("08001: network unreachable"))
    with patch.dict(sys.modules, {"pyodbc": mod}):
        with pytest.raises(McpToolError) as exc:
            PyodbcDriver().connect(CREDS, SETTINGS)
    assert exc.value.code == "GAUSSDB_DATA_MCP_CONNECT_FAILED"


# ---- 退出规避（server._odbc_exit_guard）----

def test_exit_guard_skips_gc_on_win32_pyodbc(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    events = []
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(flush=lambda: events.append("stdout")))
    monkeypatch.setattr(sys, "stderr", SimpleNamespace(flush=lambda: events.append("stderr")))
    monkeypatch.setattr(os, "_exit", lambda code: events.append(("exit", code)))
    server_mod._odbc_exit_guard()
    # flush 必须先于 _exit：os._exit 不走缓冲区清理
    assert events == ["stdout", "stderr", ("exit", 0)]


def test_exit_guard_not_triggered_otherwise(monkeypatch):
    exit_mock = MagicMock()
    monkeypatch.setattr(os, "_exit", exit_mock)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DRIVER", "psycopg2")  # win32 但非 pyodbc
    server_mod._odbc_exit_guard()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_DRIVER", "pyodbc")    # pyodbc 但非 win32
    server_mod._odbc_exit_guard()
    exit_mock.assert_not_called()
