"""连接池：读连接复用（RESET ALL 归还），写/DDL 连接用完即关。

连接建立经 drivers.get_driver() 双轨分发（psycopg2 / pyodbc），连接细节、
平台选择与认证失败映射见 drivers.py。认证失败绝不重试：密码连错 10 次
锁号 1 天（survey/03 §7.7）。
"""

import ipaddress
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from huaweicloud_mcp_core.errors import McpToolError

from .config import DataMcpSettings
from .credentials import DbCredentials
from .drivers import _cursor, get_driver

_IDLE_TTL_SECONDS = 600  # 服务端 session_timeout 默认 1800s，池内空闲必须更短

# 连接失败且 host 是裸 IP 时的自愈指引（issue #2）：放在 drivers 层之外的统一失败
# 包装处，psycopg2/pyodbc 两轨共用。数据面不持 AK/SK（凭证域分离），不做管控面
# API 自动解析 endpoint；域名可跟随 EIP 变更，见 README §3.2。
_EIP_HINT = (
    "If the instance was recreated or its EIP changed, update 'host' in "
    "credentials.json (takes effect on the next call, no restart). Consider "
    "using an instance domain name instead of a raw IP to survive EIP changes."
)


def _is_ip_host(host: str) -> bool:
    """host 是否为裸 IP（ipaddress 可解析，含 IPv6；与 credentials._is_public_host 同款工具）。

    域名不追加 EIP 指引：域名解析自动跟随实例变更，提示改 host 反而误导。
    """
    try:
        ipaddress.ip_address(host.strip())
    except ValueError:
        return False
    return True


class ConnectionPool:
    def __init__(self, *, pool_min: int, pool_max: int) -> None:
        self._min, self._max = pool_min, pool_max
        self._lock = threading.Lock()
        self._free: list[tuple[float, Any]] = []  # (idle_since, connection)
        self._fingerprint: str | None = None

    # ---- 对外接口 ----

    @contextmanager
    def read_connection(
        self, creds: DbCredentials, *, settings: DataMcpSettings
    ) -> Iterator[Any]:
        conn = self._checkout(creds, settings)
        try:
            with _cursor(conn) as cur:
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            yield conn
        finally:
            self._return(conn, fingerprint=creds.fingerprint)

    def run_write(self, creds: DbCredentials, *, settings: DataMcpSettings, sql: str) -> int:
        """写/DDL 通道：专用连接，autocommit，用完即关。返回受影响行数。"""
        conn = self._connect(creds, settings)
        try:
            with _cursor(conn) as cur:
                cur.execute(sql)
                return cur.rowcount
        finally:
            conn.close()

    def free_count(self) -> int:
        with self._lock:
            return len(self._free)

    # ---- 内部 ----

    def _connect(self, creds: DbCredentials, settings: DataMcpSettings) -> Any:
        try:
            return get_driver().connect(creds, settings)
        except McpToolError as exc:
            # 连接失败且 host 是裸 IP：两轨统一的失败包装处追加 EIP 自愈指引（issue #2）
            if exc.code == "GAUSSDB_DATA_MCP_CONNECT_FAILED" and _is_ip_host(creds.host):
                exc.hint = f"{exc.hint} {_EIP_HINT}" if exc.hint else _EIP_HINT
            raise

    def _checkout(self, creds: DbCredentials, settings: DataMcpSettings) -> Any:
        with self._lock:
            if self._fingerprint != creds.fingerprint:
                for _, conn in self._free:
                    try:
                        conn.close()
                    except Exception:  # 关闭失败不影响借出路径
                        pass
                self._free.clear()
                self._fingerprint = creds.fingerprint
            while self._free:
                idle_since, conn = self._free.pop()
                if time.monotonic() - idle_since <= _IDLE_TTL_SECONDS:
                    return conn
                try:
                    conn.close()
                except Exception:  # 过期连接关闭失败同样不阻塞
                    pass
        return self._connect(creds, settings)

    def _return(self, conn: Any, *, fingerprint: str) -> None:
        try:
            with _cursor(conn) as cur:
                cur.execute("RESET ALL")
                # 会话级咨询锁不随 RESET ALL 释放：池化连接会替上个请求长期持锁，
                # 归还前显式解锁（仍在同一 try 内——解锁失败走下方 close 路径）。
                cur.execute("SELECT pg_advisory_unlock_all()")
            with self._lock:
                # 指纹必须与当前池一致：借出期间发生轮换的连接绝不能入池，
                # 否则下个请求会静默拿到旧实例/旧用户的连接。
                if fingerprint == self._fingerprint and len(self._free) < self._max:
                    self._free.append((time.monotonic(), conn))
                    return
        except Exception:  # 归还路径任何失败都退化为关闭连接
            pass
        try:
            conn.close()
        except Exception:  # 已尽力关闭，吞掉二次失败
            pass
