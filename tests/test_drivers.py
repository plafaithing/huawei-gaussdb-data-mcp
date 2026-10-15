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
from gaussdb_data_mcp.drivers import Psycopg2Driver, PyodbcDriver, _cursor, get_driver

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
    mod.SQL_CHAR = 1  # 真 pyodbc.SQL_CHAR 的值（setdecoding 的类型参数）
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
    assert "SSLRootCert" not in conn_str  # require 无证书：连接串不带 CA 参数
    assert mod.connect.call_args.kwargs == {"timeout": 10, "ansi": False}  # issue #1：ansi=False
    assert conn.autocommit is True  # 实测必须：事务内 SET SESSION 不生效


def test_pyodbc_connect_sets_utf8_encoding():
    # issue #1：Windows 轨中文乱码——connect 后显式配置编码（真库实测 2026-10-15：
    # 参数必须走宽字符 UTF-16LE（SQL_C_WCHAR），窄字符 UTF-8 参数被 gsqlodbc 按
    # Windows ACP 误解入库；SQL_CHAR 读取解码按 pyodbc 默认 utf-8 固化，防默认漂移）。
    # mock 层无法验证编码语义，真库回归见 test_pyodbc_live_chinese_roundtrip
    mod, conn = _fake_pyodbc()
    with patch.dict(sys.modules, {"pyodbc": mod}):
        PyodbcDriver().connect(CREDS, SETTINGS)
    conn.setdecoding.assert_any_call(mod.SQL_CHAR, encoding="utf-8")
    conn.setencoding.assert_called_once_with(encoding="utf-16le")


# ---- pyodbc verify-ca fail-closed（issue #1：pyodbc 轨此前不校验 CA）----

VERIFY_CA_CREDS = DbCredentials(host="h", port=8000, dbname="db", user="u", password="p",
                                sslmode="verify-ca", fingerprint="fp2")
VERIFY_FULL_CREDS = DbCredentials(host="h", port=8000, dbname="db", user="u", password="p",
                                  sslmode="verify-full", fingerprint="fp3")


def test_pyodbc_verify_ca_without_cert_rejected():
    # fail-closed：不校验 CA 就不许谎称 verify-ca（与 credentials 层 CA_MISSING 同语义）
    mod, _ = _fake_pyodbc()
    with patch.dict(sys.modules, {"pyodbc": mod}):
        with pytest.raises(McpToolError) as exc:
            PyodbcDriver().connect(VERIFY_CA_CREDS, SETTINGS)
    assert exc.value.code == "GAUSSDB_DATA_MCP_CA_MISSING"
    assert "sslrootcert" in exc.value.hint


def test_pyodbc_verify_full_without_cert_rejected():
    mod, _ = _fake_pyodbc()
    with patch.dict(sys.modules, {"pyodbc": mod}):
        with pytest.raises(McpToolError) as exc:
            PyodbcDriver().connect(VERIFY_FULL_CREDS, SETTINGS)
    assert exc.value.code == "GAUSSDB_DATA_MCP_CA_MISSING"


def test_pyodbc_verify_ca_with_cert_passed_as_sslrootcert():
    creds = DbCredentials(host="h", port=8000, dbname="db", user="u", password="p",
                          sslmode="verify-ca", sslrootcert=r"C:\ca\cacert.pem",
                          fingerprint="fp4")
    mod, conn = _fake_pyodbc()
    with patch.dict(sys.modules, {"pyodbc": mod}):
        out = PyodbcDriver().connect(creds, SETTINGS)
    assert out is conn
    conn_str = mod.connect.call_args.args[0]
    assert r"SSLRootCert={C:\ca\cacert.pem}" in conn_str  # ODBC 惯例：值用花括号包裹
    assert "Sslmode=verify-ca" in conn_str


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


# ---- 真库回归（issue #1：Windows pyodbc 轨中文往返）----
# mock 层无法验证 ODBC 编码语义（ansi=False / setdecoding 只能证明"调用了"），
# 中文往返必须有真库用例。遵循 live marker 约定（与 tests/integration/test_live.py
# 一致：marker + 环境变量门控，未设门时跳过，基线不受影响）。
# 手动运行（Windows + GaussDB ODBC 驱动 + pyodbc，本地容器或云实例均可）：
#   GAUSSDB_DATA_MCP_PYODBC_LIVE=1 \
#   GAUSSDB_DATA_MCP_DB_HOST=127.0.0.1 GAUSSDB_DATA_MCP_DB_PORT=5432 \
#   GAUSSDB_DATA_MCP_DB_DBNAME=n8n_test GAUSSDB_DATA_MCP_DB_USER=appuser \
#   GAUSSDB_DATA_MCP_DB_PASSWORD=... \
#   uv run pytest tests/test_drivers.py -k pyodbc_live -v

@pytest.mark.live
@pytest.mark.skipif(
    not os.getenv("GAUSSDB_DATA_MCP_PYODBC_LIVE"),
    reason="GAUSSDB_DATA_MCP_PYODBC_LIVE is not set; "
           "needs Windows + GaussDB ODBC + a real instance.",
)
def test_pyodbc_live_chinese_roundtrip():
    settings = DataMcpSettings.from_env()
    creds = DbCredentials.load(settings)
    conn = PyodbcDriver().connect(creds, settings)
    try:
        with _cursor(conn) as cur:
            cur.execute("DROP TABLE IF EXISTS _mcp_pyodbc_cn_probe")
            cur.execute("CREATE TABLE _mcp_pyodbc_cn_probe (id int, name varchar(100))")
            cur.execute("INSERT INTO _mcp_pyodbc_cn_probe VALUES (1, ?)", "张三")
            cur.execute("SELECT name FROM _mcp_pyodbc_cn_probe WHERE id = 1")
            row = cur.fetchone()
            cur.execute("DROP TABLE _mcp_pyodbc_cn_probe")
    finally:
        conn.close()
    assert row is not None and row[0] == "张三"


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
