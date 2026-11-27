"""数据库凭证与 MCP token 的热轮换读取（对齐 core 的 credentials 模式）。

- 每次工具调用都重新读文件（原子替换即可轮换，无需重启）。
- 文件必须 0600（Windows 跳过权限位检查，与 core 一致）。
"""

import contextlib
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from huaweicloud_mcp_core.errors import McpToolError

from .config import DataMcpSettings

_PRIVATE_HOSTNAMES = {"localhost", "host.docker.internal"}

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


_REGION_ALIAS = {
    "北京四": "cn-north-4", "华北-北京四": "cn-north-4",
    "贵阳一": "cn-southwest-2", "西南-贵阳一": "cn-southwest-2",
    "上海一": "cn-east-3", "华东-上海一": "cn-east-3",
    "上海二": "cn-east-2", "华东-上海二": "cn-east-2",
    "广州": "cn-south-1", "华南-广州": "cn-south-1",
    "广州-友好用户环境": "cn-south-4", "华南-广州-友好用户环境": "cn-south-4",
    "深圳": "cn-south-2", "华南-深圳": "cn-south-2",
    "香港": "ap-southeast-1", "新加坡": "ap-southeast-3",
    "曼谷": "ap-southeast-2", "东京": "ap-southeast-4",
    "首尔": "ap-southeast-5", "莫斯科": "ru-north-4",
}


def normalize_region(region: str | None) -> str:
    """中文区域名 → region code；未知值原样返回（已是 code 的情况）。"""
    value = (region or "").strip()
    return _REGION_ALIAS.get(value, value)


@dataclass(frozen=True)
class DbCredentials:
    dbname: str
    user: str
    password: str = field(repr=False)
    fingerprint: str = ""
    # das 轨（SQL over 管控面 HTTPS，survey/07）：仅 GAUSSDB_DATA_MCP_DRIVER=das 时使用
    das_connection_id: str = ""
    das_ak: str = field(default="", repr=False)
    das_sk: str = field(default="", repr=False)
    das_project_id: str = ""
    das_region: str = "cn-north-4"
    das_schema: str = "public"
    das_host: str = ""
    instance_id: str = ""
    das_activated: bool = False

    @classmethod
    def _load_das(
        cls, pick: Callable[[str, Any], Any], settings: DataMcpSettings, instance: str | None
    ) -> "DbCredentials":
        """das 轨 v2：多实例映射（survey/07 §8）；兼容 v1 平铺结构。

        v2 结构：{ak, sk, region, default_instance, instances: {iid: {das_connection_id,
        dbname, schema, user, password, region?}}}。project_id 可省略（das 轨按
        AK/SK+region 自动查询）。connection_id 缺失但条目有 user/password 时允许
        自动注册（das 轨执行前补注册）。
        """
        secret = _read_secret_file(settings.credentials_file) if settings.credentials_file else {}
        instances = secret.get("instances") if isinstance(secret.get("instances"), dict) else None

        def env_or(name: str) -> str:
            return os.environ.get(name, "")

        if instances:
            ak = pick("ak", "") or env_or("GAUSSDB_DATA_MCP_CLOUD_AK")
            sk = pick("sk", "") or env_or("GAUSSDB_DATA_MCP_CLOUD_SK")
            region = (
                normalize_region(pick("region", "") or env_or("GAUSSDB_DATA_MCP_CLOUD_REGION"))
                or "cn-north-4"
            )
            iid = (instance or pick("default_instance", "") or "").strip()
            if not iid:
                if len(instances) == 1:
                    iid = next(iter(instances))
                else:
                    raise McpToolError(
                        "MCP_CREDENTIAL_ERROR",
                        "Multiple DAS instances configured; "
                        "pass 'instance' or set default_instance.",
                        hint=f"Configured instances: {', '.join(sorted(instances))}.",
                    )
            entry = instances.get(iid)
            if not isinstance(entry, dict):
                raise McpToolError(
                    "MCP_CREDENTIAL_ERROR",
                    f"DAS instance '{iid}' is not configured.",
                    hint=f"Configured instances: {', '.join(sorted(instances))}.",
                )
            def entry_pick(key: str, default: Any) -> Any:
                value = entry.get(key)
                return str(value) if value is not None else default
            creds = cls(
                instance_id=iid,
                dbname=entry_pick("dbname", "postgres"),
                user=entry_pick("user", ""),
                password=entry_pick("password", ""),
                das_connection_id=entry_pick("das_connection_id", ""),
                das_ak=ak, das_sk=sk,
                das_project_id=pick("project_id", "")
                or env_or("GAUSSDB_DATA_MCP_CLOUD_PROJECT_ID"),
                das_region=normalize_region(entry_pick("region", region)) or region,
                das_schema=entry_pick("schema", "public"),
            das_host=entry_pick("das_host", ""),
            das_activated=bool(entry_pick("activated", False)),
            )
        else:
            creds = cls(
                instance_id=pick("instance_id", "") or "",
                dbname=pick("dbname", "") or "postgres",
                user=pick("user", ""),
                password=pick("password", ""),
                das_connection_id=pick("das_connection_id", "") or "",
                das_ak=pick("ak", "") or env_or("GAUSSDB_DATA_MCP_CLOUD_AK"),
                das_sk=pick("sk", "") or env_or("GAUSSDB_DATA_MCP_CLOUD_SK"),
                das_project_id=pick("project_id", "")
                or env_or("GAUSSDB_DATA_MCP_CLOUD_PROJECT_ID"),
                das_region=normalize_region(
                    pick("region", "") or env_or("GAUSSDB_DATA_MCP_CLOUD_REGION")
                ) or "cn-north-4",
                das_schema=pick("schema", "") or "public",
                das_host=pick("das_host", ""),
                das_activated=bool(pick("activated", False)),
            )
        missing = [
            k for k, v in (("ak", creds.das_ak), ("sk", creds.das_sk), ("region", creds.das_region),
                           ("password", creds.password))
            if not v
        ]
        if missing:
            raise McpToolError(
                "MCP_CREDENTIAL_ERROR",
                f"Missing DAS credentials: {', '.join(missing)}.",
                hint="Set ak/sk/region in the credentials file "
                "or GAUSSDB_DATA_MCP_CLOUD_* variables.",
            )
        canonical = json.dumps(
            {"conn": creds.das_connection_id, "project": creds.das_project_id,
             "region": creds.das_region, "db": creds.dbname, "schema": creds.das_schema,
             "instance": creds.instance_id},
            sort_keys=True,
        )
        return replace(creds, fingerprint=hashlib.sha256(canonical.encode()).hexdigest()[:16])

    @classmethod
    def load(cls, settings: DataMcpSettings, instance: str | None = None) -> "DbCredentials":
        secret = _read_secret_file(settings.credentials_file) if settings.credentials_file else {}

        def pick(file_key: str, env_value: Any) -> Any:
            value = secret.get(file_key)
            return str(value) if value is not None else env_value

        return cls._load_das(pick, settings, instance)


def save_das_connection(path: str, instance_id: str, connection_id: str,
                        dbname: str | None = None, activated: bool | None = None) -> None:
    """把 RegisterDbUser 产物原子写回 credentials.json（v2 instances 结构；0600 保持）。

    v1 平铺结构遇到时升级为 v2（把平铺 das_connection_id 迁入 instances）。调用方负责
    处理并发（MCP 单进程内由调用点加锁）；每次调用重读文件（与热轮换兼容）。
    """
    secret: dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8") as f:
            secret = json.load(f)
    except FileNotFoundError:
        pass
    if not isinstance(secret.get("instances"), dict):
        legacy_conn = secret.get("das_connection_id")
        secret["instances"] = {}
        if legacy_conn and secret.get("instance_id"):
            secret["instances"][str(secret["instance_id"])] = {"das_connection_id": legacy_conn}
    instances = secret.setdefault("instances", {})
    entry = instances.setdefault(str(instance_id), {})
    entry["das_connection_id"] = str(connection_id)
    if dbname:
        entry["dbname"] = dbname
    if activated is not None:
        entry["activated"] = bool(activated)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(secret, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


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
