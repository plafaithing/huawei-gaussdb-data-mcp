"""GAUSSDB_DATA_MCP_* 配置。与 core 的 HUAWEICLOUD_MCP_* 心智对齐，但默认值更安全。"""

import os
from dataclasses import dataclass, field

from huaweicloud_mcp_core.errors import McpToolError


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise McpToolError("MCP_INVALID_ARGS", f"{name} must be true or false.")
    return value in {"true", "1", "yes"}


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
    statement_timeout_ms: int = 30000
    # 连接池
    pool_min: int = 1
    pool_max: int = 8
    application_name: str = "gaussdb_data_mcp"
    # DB 环境变量兜底（credentials 文件优先；password 不进 repr）
    db_host: str = ""
    db_port: int = 0
    db_dbname: str = ""
    db_user: str = ""
    db_password: str = field(default="", repr=False)
    db_sslmode: str = "prefer"
    db_sslrootcert: str | None = None
    db_sslcert: str | None = None
    db_sslkey: str | None = None

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
            server_port=int(os.getenv("MCP_SERVER_PORT", "8000")),
            max_rows=int(os.getenv("GAUSSDB_DATA_MCP_MAX_ROWS", "200")),
            max_cell_chars=int(os.getenv("GAUSSDB_DATA_MCP_MAX_CELL_CHARS", "256")),
            max_sql_chars=int(os.getenv("GAUSSDB_DATA_MCP_MAX_SQL_CHARS", "65536")),
            statement_timeout_ms=int(os.getenv("GAUSSDB_DATA_MCP_STATEMENT_TIMEOUT_MS", "30000")),
            pool_min=int(os.getenv("GAUSSDB_DATA_MCP_POOL_MIN", "1")),
            pool_max=int(os.getenv("GAUSSDB_DATA_MCP_POOL_MAX", "8")),
            application_name=os.getenv("GAUSSDB_DATA_MCP_APPLICATION_NAME", "gaussdb_data_mcp"),
            db_host=os.getenv("GAUSSDB_DATA_MCP_DB_HOST", ""),
            db_port=int(os.getenv("GAUSSDB_DATA_MCP_DB_PORT", "0")),
            db_dbname=os.getenv("GAUSSDB_DATA_MCP_DB_DBNAME", ""),
            db_user=os.getenv("GAUSSDB_DATA_MCP_DB_USER", ""),
            db_password=os.getenv("GAUSSDB_DATA_MCP_DB_PASSWORD", ""),
            db_sslmode=os.getenv("GAUSSDB_DATA_MCP_DB_SSLMODE", "prefer"),
            db_sslrootcert=os.getenv("GAUSSDB_DATA_MCP_DB_SSLROOTCERT"),
            db_sslcert=os.getenv("GAUSSDB_DATA_MCP_DB_SSLCERT"),
            db_sslkey=os.getenv("GAUSSDB_DATA_MCP_DB_SSLKEY"),
        )
        if not 1 <= settings.server_port <= 65535:
            raise McpToolError("MCP_INVALID_ARGS", "MCP_SERVER_PORT must be 1..65535.")
        if settings.max_rows < 1 or settings.statement_timeout_ms < 1:
            raise McpToolError("MCP_INVALID_ARGS", "MAX_ROWS / STATEMENT_TIMEOUT_MS must be >= 1.")
        if settings.pool_min < 1 or settings.pool_max < settings.pool_min:
            raise McpToolError("MCP_INVALID_ARGS", "POOL_MIN must be >=1 and <= POOL_MAX.")
        return settings
