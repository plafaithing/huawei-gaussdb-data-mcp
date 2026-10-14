"""数据库凭证与 MCP token 的热轮换读取（对齐 core 的 credentials 模式）。

- 每次工具调用都重新读文件（原子替换即可轮换，无需重启）。
- 文件必须 0600（Windows 跳过权限位检查，与 core 一致）。
- port 无默认值：GaussDB CN 端口必须显式配置（驱动默认 5432 是错的）。
- 模式 2 护栏：公网地址 + sslmode 弱于 require → 拒绝（fail-closed）。
- verify-ca/verify-full 且未显式配 sslrootcert → 自动探测约定路径的 cacert.pem
  （credentials.json 同目录；无凭证文件时当前工作目录），找不到即拒绝。
"""

import hashlib
import ipaddress
import json
import os
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from huaweicloud_mcp_core.errors import McpToolError

from .config import DataMcpSettings

_PRIVATE_HOSTNAMES = {"localhost", "host.docker.internal"}
_STRONG_SSLMODES = {"require", "verify-ca", "verify-full"}
_VERIFY_SSLMODES = {"verify-ca", "verify-full"}


def _conventional_ca_path(credentials_file: str | None) -> Path:
    """约定 CA 证书路径：credentials.json 同目录（无凭证文件则 cwd）下的 cacert.pem。"""
    base = Path(credentials_file).expanduser().parent if credentials_file else Path.cwd()
    return base / "cacert.pem"


def _read_secret_file(path: str) -> dict[str, Any]:
    try:
        with Path(path).expanduser().open("r", encoding="utf-8") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode) or (os.name != "nt" and mode & 0o077):
                raise ValueError("file must be regular with mode 0600 or stricter")
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise McpToolError(
            "MCP_CREDENTIAL_ERROR",
            "The hot-reload credential file is unavailable or invalid.",
            hint=f"Atomically replace '{path}' with valid JSON and mode 0600 ({exc}).",
        ) from None
    if not isinstance(value, dict):
        raise McpToolError("MCP_CREDENTIAL_ERROR", "Credential file must be a JSON object.")
    return value


def _is_public_host(host: str) -> bool:
    """公网地址判定：回环/私网/链路本地/保留地址按内网；域名一律按公网（保守）。

    接受 CPython 标准库 is_private 语义（含 2001:db8::/32 文档段被归内网）；该段
    不可全球路由、连接必然失败，判定偏内网是安全方向。
    """
    if not host:
        return False
    if host.strip().lower() in _PRIVATE_HOSTNAMES:
        return False
    try:
        addr = ipaddress.ip_address(host.strip())
    except ValueError:
        return True
    return not (addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved)


@dataclass(frozen=True)
class DbCredentials:
    host: str
    port: int
    dbname: str
    user: str
    password: str = field(repr=False)
    sslmode: str = "prefer"
    sslrootcert: str | None = None
    sslcert: str | None = None
    sslkey: str | None = None
    fingerprint: str = ""

    @property
    def connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self.host, "port": self.port, "dbname": self.dbname,
            "user": self.user, "password": self.password, "sslmode": self.sslmode,
        }
        for key in ("sslrootcert", "sslcert", "sslkey"):
            if getattr(self, key):
                kwargs[key] = getattr(self, key)
        return kwargs

    @classmethod
    def load(cls, settings: DataMcpSettings) -> "DbCredentials":
        secret = _read_secret_file(settings.credentials_file) if settings.credentials_file else {}

        def pick(file_key: str, env_value: Any) -> Any:
            value = secret.get(file_key)
            return str(value) if value is not None else env_value

        try:
            port = int(pick("port", settings.db_port) or 0)
        except (TypeError, ValueError):
            raise McpToolError(
                "MCP_CREDENTIAL_ERROR",
                "Database port must be an integer.",
                hint="Set 'port' in the credentials file or GAUSSDB_DATA_MCP_DB_PORT.",
            ) from None
        if port <= 0:
            raise McpToolError(
                "MCP_CREDENTIAL_ERROR",
                "Database port is required and has no default.",
                hint="GaussDB CN port is usually 8000; the driver default 5432 is wrong.",
            )
        creds = cls(
            host=pick("host", settings.db_host),
            port=port,
            dbname=pick("dbname", settings.db_dbname),
            user=pick("user", settings.db_user),
            password=pick("password", settings.db_password),
            sslmode=pick("sslmode", settings.db_sslmode) or "prefer",
            sslrootcert=pick("sslrootcert", settings.db_sslrootcert) or None,
            sslcert=pick("sslcert", settings.db_sslcert) or None,
            sslkey=pick("sslkey", settings.db_sslkey) or None,
        )
        missing = [k for k, v in (("host", creds.host), ("dbname", creds.dbname),
                                  ("user", creds.user), ("password", creds.password)) if not v]
        if missing:
            raise McpToolError(
                "MCP_CREDENTIAL_ERROR",
                f"Missing database credentials: {', '.join(missing)}.",
                hint="Set GAUSSDB_DATA_MCP_CREDENTIALS_FILE or GAUSSDB_DATA_MCP_DB_* variables.",
            )
        if _is_public_host(creds.host) and creds.sslmode not in _STRONG_SSLMODES:
            raise McpToolError(
                "GAUSSDB_DATA_MCP_INSECURE_PUBLIC",
                f"Public host '{creds.host}' requires sslmode require/verify-ca/verify-full "
                f"(got '{creds.sslmode}').",
                hint="Use a VPC-internal address, or set sslmode to verify-ca in credentials.",
            )
        if creds.sslmode in _VERIFY_SSLMODES and not creds.sslrootcert:
            # 零配置 CA：未显式配 sslrootcert → 探测约定路径，找不到 fail-closed
            ca_path = _conventional_ca_path(settings.credentials_file)
            if ca_path.is_file():
                creds = replace(creds, sslrootcert=str(ca_path))
            else:
                raise McpToolError(
                    "GAUSSDB_DATA_MCP_CA_MISSING",
                    f"sslmode '{creds.sslmode}' requires a CA certificate, "
                    "but none was configured or found.",
                    hint=f"Download the CA certificate from the console SSL tab and save it "
                    f"as '{ca_path.resolve()}', or set sslrootcert explicitly.",
                )
        canonical = json.dumps(creds.connect_kwargs, sort_keys=True)
        return replace(creds, fingerprint=hashlib.sha256(canonical.encode()).hexdigest()[:16])


def load_tokens(path: str) -> dict[str, str]:
    """读取 {"tokens": {client: token}}；每次调用重读（热轮换）。

    token 值仅支持 ASCII 可打印字符：HTTP 头是 latin-1/ASCII 语义，非 ASCII 或
    控制字符在加载层直接拒绝（fail-fast），好过请求期静默 401 或编码歧义。
    """
    value = _read_secret_file(path)
    tokens = value.get("tokens")
    if not isinstance(tokens, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in tokens.items()
    ):
        raise McpToolError("MCP_CREDENTIAL_ERROR", 'tokens.json must be {"tokens": {name: token}}.')
    for name, token in tokens.items():
        if not token.isascii() or not token.isprintable():
            raise McpToolError(
                "MCP_CREDENTIAL_ERROR",
                f"Token '{name}' must be printable ASCII.",
                hint="Generate tokens from [A-Za-z0-9._~-] (RFC 6750 b64token charset).",
            )
    return dict(tokens)
