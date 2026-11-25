"""GAUSSDB_DATA_MCP_* 配置（das-only：唯一传输为 DAS 管控面 HTTPS）。"""

import os
from dataclasses import dataclass

from huaweicloud_mcp_core.errors import McpToolError


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise McpToolError("MCP_INVALID_ARGS", f"{name} must be true or false.")
    return value in {"true", "1", "yes"}


def _env_int(name: str, default: str) -> int:
    # 裸 int() 对非数字抛 ValueError 裸栈；统一映射为 McpToolError（issue #1）
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except ValueError:
        raise McpToolError("MCP_INVALID_ARGS", f"{name} must be an integer.") from None


def _env_csv(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


@dataclass(frozen=True)
class DataMcpSettings:
    # 安全开关（用户决策：写能力一期可用，但代码级默认值必须安全）
    read_only: bool = True
    enable_ddl: bool = False
    # 文件
    credentials_file: str | None = None
    tokens_file: str | None = None
    guardrails_config_path: str | None = None
    audit_file: str | None = None
    # guardrails 资源保护（传给 core load_rules）
    protected_ids: tuple[str, ...] = ()
    protected_names: tuple[str, ...] = ()
    protected_tags: tuple[str, ...] = ()
    # HTTP 监听（默认回环；非回环 + 无 tokens → 拒绝启动，见 server.py）
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    # 资源限制
    max_rows: int = 200
    max_cell_chars: int = 256
    max_sql_chars: int = 65536

    @classmethod
    def from_env(cls) -> "DataMcpSettings":
        settings = cls(
            read_only=_env_bool("GAUSSDB_DATA_MCP_READ_ONLY", True),
            enable_ddl=_env_bool("GAUSSDB_DATA_MCP_ENABLE_DDL", False),
            credentials_file=os.getenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE"),
            tokens_file=os.getenv("GAUSSDB_DATA_MCP_TOKENS_FILE"),
            guardrails_config_path=os.getenv("GAUSSDB_DATA_MCP_GUARDRAILS_CONFIG"),
            audit_file=os.getenv("GAUSSDB_DATA_MCP_AUDIT_FILE"),
            protected_ids=_env_csv("GAUSSDB_DATA_MCP_PROTECTED_IDS"),
            protected_names=_env_csv("GAUSSDB_DATA_MCP_PROTECTED_NAMES"),
            protected_tags=_env_csv("GAUSSDB_DATA_MCP_PROTECTED_TAGS"),
            server_host=os.getenv("MCP_SERVER_HOST", "127.0.0.1"),
            server_port=_env_int("MCP_SERVER_PORT", "8000"),
            max_rows=_env_int("GAUSSDB_DATA_MCP_MAX_ROWS", "200"),
            max_cell_chars=_env_int("GAUSSDB_DATA_MCP_MAX_CELL_CHARS", "256"),
            max_sql_chars=_env_int("GAUSSDB_DATA_MCP_MAX_SQL_CHARS", "65536"),
        )
        if not 1 <= settings.server_port <= 65535:
            raise McpToolError("MCP_INVALID_ARGS", "MCP_SERVER_PORT must be 1..65535.")
        if settings.max_rows < 1:
            raise McpToolError("MCP_INVALID_ARGS", "MAX_ROWS must be >= 1.")
        return settings
