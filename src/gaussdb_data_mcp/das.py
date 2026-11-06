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
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterator

import requests
from huaweicloud_mcp_core.errors import McpToolError

from .credentials import DbCredentials

_EXECUTE_PATH = "/v3/{project}/connections/{conn}/async-execute-sql-without-key"
_RESULT_PATH = "/v3/{project}/connections/{conn}/get-execute-result-without-key"
_LOGIN_PATH = "/v3/{project}/connections/{conn}/login-actions"
_REGISTER_PATH = "/v3/{project}/instances/{iid}/db-users"
_REGISTER_DATASTORE = "gaussdbv5"
_IAM_HOST = "iam.myhuaweicloud.com"
_TIMEOUT = 30
# DDL 语句识别（大小写不敏感）：pg 系统库中执行 DDL 需要 panelKey（见 execute 注释）
_DDL_RE = re.compile(r"^\s*(CREATE|DROP|ALTER|TRUNCATE|COMMENT|RENAME)\b", re.IGNORECASE)


def _url_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="~")


def _die(code: str, message: str, hint: str | None = None) -> McpToolError:
    return McpToolError(code, message, hint=hint) if hint else McpToolError(code, message)


class _DasHttp:
    """签名 HTTP：CanonicalURI 尾补 / + SDK-HMAC-SHA256（与官方 SDK 同算法）。"""

    def __init__(self, host: str, ak: str, sk: str) -> None:
        self._host = host
        self._ak = ak
        self._sk = sk

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._send("POST", path, json.dumps(body), [])


    def get(self, host: str, path: str, query: list[tuple[str, str]]) -> dict[str, Any]:
        # GET 签名：query 参与规范化（按 key 排序、URL 编码）
        return self._send("GET", path, "", query, host=host)


    def _send(self, method: str, path: str, body_str: str, query: list[tuple[str, str]],
              host: str | None = None) -> dict[str, Any]:
        host = host or self._host
        date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        canonical_uri = path if path.endswith("/") else path + "/"
        canonical_query = "&".join(
            f"{_url_quote(k)}={_url_quote(v)}" for k, v in sorted(query)
        )
        canonical_headers = f"content-type:application/json\nhost:{host}\nx-sdk-date:{date}\n"
        signed_headers = "content-type;host;x-sdk-date"
        payload_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
        canonical_request = (
            f"{method}\n{canonical_uri}\n{canonical_query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
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
        qs = f"?{canonical_query}" if canonical_query else ""
        resp = requests.request(
            method,
            f"https://{host}{path}{qs}",
            data=body_str.encode("utf-8") if body_str else None,
            headers={
                "content-type": "application/json",
                "X-Sdk-Date": date,
                "Host": host,
                "Authorization": authorization,
            },
            timeout=_TIMEOUT,
        )
        try:
            return resp.json()
        except ValueError:
            # login-actions 等接口成功时返回 200 空体——非 JSON 不视为错误
            return {"_status": resp.status_code, "_text": resp.text[:200]}


class DasBackend:
    """与 ConnectionPool 同接口的 DAS 后端：read_connection/run_write/free_count。"""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._project_cache: dict[tuple[str, str], str] = {}

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
        if creds.das_project_id:
            return creds.das_project_id
        cache_key = (creds.das_ak, creds.das_region)
        with self._lock:
            cached = self._project_cache.get(cache_key)
        if cached:
            return cached
        resp = self._http(creds).get(_IAM_HOST, "/v3/projects", [("name", creds.das_region)])
        projects = resp.get("projects") or []
        pid = (projects[0] or {}).get("id") if projects else ""
        if not pid:
            raise _die(
                "GAUSSDB_DATA_MCP_DAS_PROJECT_UNRESOLVED",
                f"Cannot resolve project_id for region '{creds.das_region}': "
                f"{json.dumps(resp, ensure_ascii=False)[:200]}",
            )
        with self._lock:
            self._project_cache[cache_key] = pid
        return pid

    def register_db_user(self, creds: DbCredentials, instance_id: str) -> str:
        """DAS RegisterDbUser（datastore_type=gaussdbv5，survey/07 §8）；返回 db_user_id。"""
        resp = self._http(creds).post(
            _REGISTER_PATH.format(project=self._project(creds), iid=instance_id),
            {"db_username": creds.user, "db_user_password": creds.password,
             "datastore_type": _REGISTER_DATASTORE},
        )
        uid = resp.get("db_user_id")
        if not uid:
            raise _die(
                "GAUSSDB_DATA_MCP_DAS_REGISTER_FAILED",
                f"DAS register rejected: {json.dumps(resp, ensure_ascii=False)[:300]}",
            )
        return uid

    def _ensure_connection(self, creds: DbCredentials, settings: Any) -> DbCredentials:
        """connection_id 缺失：自动 RegisterDbUser 并原子写回 credentials.json（§8）。"""
        if not (creds.instance_id and creds.user and creds.password):
            raise _die(
                "GAUSSDB_DATA_MCP_DAS_NO_CONNECTION",
                "das driver requires das_connection_id; auto-register needs "
                "instance_id/user/password in the instance entry.",
            )
        new_id = self.register_db_user(creds, creds.instance_id)
        if settings.credentials_file:
            from .credentials import save_das_connection
            save_das_connection(
                settings.credentials_file, creds.instance_id, new_id, creds.dbname or None
            )
        return replace(creds, das_connection_id=new_id)

    def execute(self, creds: DbCredentials, settings: Any, sql: str) -> dict[str, Any]:
        """两步异步：执行受理 → 立即取结果。返回原始驼峰响应 dict。

        硬约束（研发 2026-11-06 确认）：受理与取结果两个接口之间**不得有任何
        时间间隔**——结果有时效，本方法内部禁止插入 sleep/节流/其他逻辑。
        """
        if not creds.dbname:
            raise _die("GAUSSDB_DATA_MCP_DAS_NO_DATABASE", "das driver requires database_name.")
        if not creds.das_connection_id:
            creds = self._ensure_connection(creds, settings)  # 幂等：映射缺失才注册
        http = self._http(creds)
        conn = creds.das_connection_id
        body = {
            "database_name": creds.dbname,
            "schema_name": creds.das_schema,
            "sql_script": sql,
            "curpage": 1,
            "perpage": settings.max_rows,
            "ignore_limit": False,
        }
        # panelKey（研发方案 1，2026-11-06）：**仅在 pg 系统库执行 DDL 时附加**——
        # GaussDB 禁止事务中建库（DAS 吞错伪装成功），且实测 pg 库中 CREATE TABLE
        # 等全部 DDL 均挂死（60s 无响应）；panelKey 走非事务上下文实测生效。
        # 普通 SQL 不带——panelKey 改变执行上下文，可能造成数据不一致（用户决策）。
        if creds.dbname == "postgres" and _DDL_RE.match(sql):
            body["panelKey"] = str(uuid.uuid4())
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
        # login(username-only) 自愈（DAS 用托管密码，零密码输入）后整链重试；5018 同走此路
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
        self._check_sql_error(result)
        return result

    @staticmethod
    def _check_sql_error(result: dict[str, Any]) -> None:
        """DAS 把 SQL 执行错误吞进 errorMessage 且 errorCodeStr/executeStatus 仍为
        成功态（实测："CREATE DATABASE cannot be executed in a transaction block."）
        ——errorMessage 非空即视为执行失败，fail-fast。"""
        err = result.get("errorMessage")
        if err:
            raise _die("GAUSSDB_DATA_MCP_SQL_ERROR", f"DAS SQL error: {err}")

    def _relogin(self, creds: DbCredentials, http: _DasHttp, conn: str) -> None:
        """会话自愈：login(username-only)——DAS 用注册时托管的密码，**零密码输入**。

        仅当 DAS 报业务错（如未存密码/密码已改）且本地有 password 时才降级
        RetryLoginInfo（带密码重存）。
        """
        with self._lock:
            resp = http.post(
                _LOGIN_PATH.format(project=self._project(creds), conn=conn),
                {"login": {"username": creds.user}},
            )
            failed = bool(resp.get("errorCodeStr")) or resp.get("_status", 200) >= 400
            if failed and creds.password:
                http.post(
                    _LOGIN_PATH.format(project=self._project(creds), conn=conn),
                    {"retry_login": {"username": creds.user, "password": creds.password,
                                     "is_save_password": True}},
                )


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
