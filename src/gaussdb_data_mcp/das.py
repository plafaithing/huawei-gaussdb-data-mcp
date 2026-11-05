"""DAS 数据面驱动轨：SQL over 管控面 HTTPS（数据库端口零公网可达）。

契约见 survey/07：
- 执行 POST /v3/{project}/connections/{conn}/async-execute-sql-without-key
  body {database_name✱, schema_name✱, sql_script✱, curpage, perpage, ignore_limit:false}
  → {"success": true, "executeId": "..."}
- 取结果 POST .../get-execute-result-without-key body {"execute_id": ...}——须执行后立即调用
  （结果有时效且消费型，取一次即空）
- 响应全驼峰：rows / metadata[{label,...}] / columnValues[{COLUMN_N: value}] / executeStatus /
  timeDelay / bigTable / exceedData——SDK model 会丢驼峰字段，必须直解析原始 JSON
- 列名还原：columnValues 行内键 COLUMN_N → metadata[N-1].label
- DAS.5018 会话失效：execute_login_connection_new(retry_login) 自愈一次后重试
- 签名：SDK-HMAC-SHA256；CanonicalURI 末尾强制补 "/"（SDK 隐藏规范）
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import requests
from huaweicloud_mcp_core.errors import McpToolError

from .credentials import DbCredentials

_EXECUTE_PATH = "/v3/{project}/connections/{conn}/async-execute-sql-without-key"
_RESULT_PATH = "/v3/{project}/connections/{conn}/get-execute-result-without-key"
_LOGIN_PATH = "/v3/{project}/connections/{conn}/login"
_TIMEOUT = 30


def _die(code: str, message: str) -> McpToolError:
    return McpToolError(code, message)


class _DasHttp:
    """签名 HTTP：CanonicalURI 尾补 / + SDK-HMAC-SHA256（与官方 SDK 同算法）。"""

    def __init__(self, host: str, ak: str, sk: str) -> None:
        self._host = host
        self._ak = ak
        self._sk = sk

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        body_str = json.dumps(body)
        date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        canonical_uri = path if path.endswith("/") else path + "/"
        canonical_headers = f"content-type:application/json\nhost:{self._host}\nx-sdk-date:{date}\n"
        signed_headers = "content-type;host;x-sdk-date"
        payload_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
        canonical_request = (
            f"POST\n{canonical_uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )
        string_to_sign = (
            f"SDK-HMAC-SHA256\n{date}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )
        signature = hmac.new(
            self._sk.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        authorization = (
            f"SDK-HMAC-SHA256 Access={self._ak}, SignedHeaders={signed_headers}, Signature={signature}"
        )
        resp = requests.post(
            f"https://{self._host}{path}",
            data=body_str.encode("utf-8"),
            headers={
                "content-type": "application/json",
                "X-Sdk-Date": date,
                "Host": self._host,
                "Authorization": authorization,
            },
            timeout=_TIMEOUT,
        )
        try:
            return resp.json()
        except ValueError as exc:
            raise _die("GAUSSDB_DATA_MCP_DAS_HTTP", f"DAS returned non-JSON HTTP {resp.status_code}") from exc


class DasBackend:
    """与 ConnectionPool 同接口的 DAS 后端：read_connection/run_write/free_count。"""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._lock = threading.Lock()

    # -- 池接口（das 模式无池：free_count 恒 0） --------------------------------
    def free_count(self) -> int:
        return 0

    @contextmanager
    def read_connection(self, creds: DbCredentials, *, settings: Any) -> Iterator["DasConnection"]:
        yield DasConnection(self, creds, settings)

    def run_write(self, creds: DbCredentials, *, settings: Any, sql: str) -> int:
        cur = DasCursor(self, creds, settings)
        cur.execute(sql)
        return cur.affected_rows

    # -- DAS 执行 ---------------------------------------------------------------
    def _http(self, creds: DbCredentials) -> _DasHttp:
        return _DasHttp(
            host=f"das.{creds.das_region}.myhuaweicloud.com", ak=creds.das_ak, sk=creds.das_sk
        )

    def _project(self, creds: DbCredentials) -> str:
        return creds.das_project_id

    def _conn(self, creds: DbCredentials) -> str:
        if not creds.das_connection_id:
            raise _die(
                "GAUSSDB_DATA_MCP_DAS_NO_CONNECTION",
                "das driver requires credentials field das_connection_id (from RegisterDbUser).",
            )
        return creds.das_connection_id

    def execute(self, creds: DbCredentials, settings: Any, sql: str) -> dict[str, Any]:
        """两步异步：执行受理 → 立即取结果。返回原始驼峰响应 dict。"""
        http = self._http(creds)
        conn = self._conn(creds)
        body = {
            "database_name": creds.dbname,
            "schema_name": creds.das_schema,
            "sql_script": sql,
            "curpage": 1,
            "perpage": settings.max_rows,
            "ignore_limit": False,
        }
        if not creds.dbname:
            raise _die("GAUSSDB_DATA_MCP_DAS_NO_DATABASE", "das driver requires database_name.")
        resp = http.post(_EXECUTE_PATH.format(project=self._project(creds), conn=conn), body)
        exec_id = resp.get("executeId")
        if not resp.get("success") or not exec_id:
            raise _die(
                "GAUSSDB_DATA_MCP_DAS_EXECUTE_FAILED",
                f"DAS execute rejected: {json.dumps(resp, ensure_ascii=False)[:300]}",
            )
        result = http.post(
            _RESULT_PATH.format(project=self._project(creds), conn=conn), {"execute_id": exec_id}
        )
        # 空壳检测：未激活过的连接（或会话失效）会返回 200 空响应（无报错）——
        # retry_login 自愈一次后整链重试；5018 同走此路
        if not (result.get("metadata") or result.get("columnValues") or result.get("executeStatus")):
            self._relogin(creds, http, conn)
            resp = http.post(_EXECUTE_PATH.format(project=self._project(creds), conn=conn), body)
            exec_id = resp.get("executeId")
            if not resp.get("success") or not exec_id:
                raise _die(
                    "GAUSSDB_DATA_MCP_DAS_EXECUTE_FAILED",
                    f"DAS execute rejected after relogin: {json.dumps(resp, ensure_ascii=False)[:300]}",
                )
            result = http.post(
                _RESULT_PATH.format(project=self._project(creds), conn=conn), {"execute_id": exec_id}
            )
        return result

    def _relogin(self, creds: DbCredentials, http: _DasHttp, conn: str) -> None:
        with self._lock:
            body = {
                "retry_login": {
                    "username": creds.user,
                    "password": creds.password,
                    "is_save_password": True,
                }
            }
            http.post(_LOGIN_PATH.format(project=self._project(creds), conn=conn), body)


class DasConnection:
    """连接句柄（conn.cursor() 契约）；das 模式无会话状态，cursor 即完整执行。"""

    def __init__(self, backend: DasBackend, creds: DbCredentials, settings: Any) -> None:
        self._backend = backend
        self._creds = creds
        self._settings = settings

    def cursor(self) -> "DasCursor":
        return DasCursor(self._backend, self._creds, self._settings)

    def __enter__(self) -> "DasConnection":
        return self

    def __exit__(self, *a: Any) -> None:
        return None


class DasDriver:
    """驱动元信息（drivers.get_driver 的 das 分支）：与 Psycopg2Driver 同接口。"""

    def driver_name(self) -> str:
        return "das"

    def placeholder(self) -> str:
        return "%s"


class DasCursor:
    """符合既有 cursor 契约（description / fetchmany / fetchone / fetchall）。

    每次 execute 都是一次完整 DAS 两步调用（无服务端会话状态）。
    """

    def __init__(self, backend: DasBackend, creds: DbCredentials, settings: Any) -> None:
        self._backend = backend
        self._creds = creds
        self._settings = settings
        self._columns: list[str] = []
        self._rows: list[tuple] = []
        self.affected_rows = 0
        self.description: list[tuple] | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        if params:
            # 仅 metadata 工具会带参数（schema/table 已过 _IDENTIFIER 白名单）；
            # 内联时仍做转义，双保险。DAS 命名参数化待契约验证后启用。
            for p in params:
                literal = str(p) if isinstance(p, (int, float)) else "'" + str(p).replace("'", "''") + "'"
                sql = sql.replace("%s", literal, 1)
        result = self._backend.execute(self._creds, self._settings, sql)
        meta = result.get("metadata") or []
        self._columns = [m.get("label") or m.get("name") or "" for m in meta]
        raw_rows = result.get("columnValues") or []
        self._rows = [
            tuple(row.get(f"COLUMN_{i + 1}") for i in range(len(meta))) for row in raw_rows
        ]
        self.affected_rows = int(result.get("rows") or 0)
        self.description = [(c, None, None, None, None, None, None) for c in self._columns]

    def fetchmany(self, size: int) -> list[tuple]:
        out, self._rows = self._rows[:size], self._rows[size:]
        return out

    def fetchone(self) -> tuple | None:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[tuple]:
        out, self._rows = self._rows, []
        return out

    def close(self) -> None:
        self._rows = []
        self.description = None

    def __enter__(self) -> "DasCursor":
        return self

    def __exit__(self, *a: Any) -> None:
        return None


_backend: DasBackend | None = None
_backend_lock = threading.Lock()


def get_das_backend(settings: Any) -> DasBackend:
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = DasBackend(settings)
        return _backend
