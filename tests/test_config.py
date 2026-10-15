import pytest
from huaweicloud_mcp_core.errors import McpToolError

from gaussdb_data_mcp.config import DataMcpSettings


def test_safe_defaults(monkeypatch):
    monkeypatch.delenv("GAUSSDB_DATA_MCP_READ_ONLY", raising=False)
    monkeypatch.delenv("GAUSSDB_DATA_MCP_ENABLE_DDL", raising=False)
    monkeypatch.delenv("MCP_SERVER_HOST", raising=False)
    monkeypatch.delenv("MCP_SERVER_PORT", raising=False)
    s = DataMcpSettings.from_env()
    assert s.read_only is True          # core 的弱点：默认 False；我们代码级默认只读
    assert s.enable_ddl is False
    assert s.server_host == "127.0.0.1"  # core 的弱点：默认 0.0.0.0；我们默认回环
    assert s.server_port == 8000
    assert s.max_rows == 200
    assert s.statement_timeout_ms == 30000


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("GAUSSDB_DATA_MCP_READ_ONLY", "false")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_ENABLE_DDL", "true")
    monkeypatch.setenv("GAUSSDB_DATA_MCP_MAX_ROWS", "10")
    s = DataMcpSettings.from_env()
    assert s.read_only is False and s.enable_ddl is True and s.max_rows == 10


def test_bad_bool_rejected(monkeypatch):
    monkeypatch.setenv("GAUSSDB_DATA_MCP_READ_ONLY", "maybe")
    with pytest.raises(McpToolError):
        DataMcpSettings.from_env()


def test_bad_port_rejected(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_PORT", "70000")
    with pytest.raises(McpToolError):
        DataMcpSettings.from_env()


@pytest.mark.parametrize("name", [
    "MCP_SERVER_PORT",
    "GAUSSDB_DATA_MCP_MAX_ROWS",
    "GAUSSDB_DATA_MCP_MAX_CELL_CHARS",
    "GAUSSDB_DATA_MCP_MAX_SQL_CHARS",
    "GAUSSDB_DATA_MCP_STATEMENT_TIMEOUT_MS",
    "GAUSSDB_DATA_MCP_POOL_MIN",
    "GAUSSDB_DATA_MCP_POOL_MAX",
    "GAUSSDB_DATA_MCP_DB_PORT",
])
def test_non_integer_env_rejected(monkeypatch, name):
    # issue #1：裸 int() 对非数字报 ValueError 裸栈；统一映射为 McpToolError
    monkeypatch.setenv(name, "abc")
    with pytest.raises(McpToolError, match="must be an integer"):
        DataMcpSettings.from_env()
