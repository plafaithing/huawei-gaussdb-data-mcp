"""审计：继承 core AuditLogger，把 SQL 文本截断从 200 放宽（诊断需要），
并附加 HTTP 客户端身份（auth 中间件写入 contextvar）。

双版本兼容（依赖范围 huaweicloud-mcp-core>=1.0,<2）：
- core 1.0.0：``emit(**event)`` kwargs 调用、masking 引擎是私有 ``_masking``；
- core 1.1.1：``emit(event_dict)`` 位置调用、masking 引擎公开为 ``masking``。
core 把 ``value[:200]`` 硬编码在方法体内，子类预截断后调 ``super().emit()``
会被二次截回 200——因此完整重写 emit，仅放宽 query 键，其余键保持 core 语义
（200 截断 + masking）。masking 引擎经属性探测获取；两个版本都没有时跳过
脱敏（审计记录仍完整落盘，降级安全）。
"""

import json
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from huaweicloud_mcp_core.guardrails.audit import AuditLogger

client_name: ContextVar[str] = ContextVar("mcp_server_gaussdb_data_client", default="stdio")


class SqlAuditLogger(AuditLogger):
    def __init__(self, file_path: str | None = None, *, sql_trunc: int = 2000) -> None:
        super().__init__(file_path=file_path)
        self.sql_trunc = sql_trunc

    def emit(self, *args: Any, **event: Any) -> None:
        # 归一两种调用形态：1.1.x 位置传 dict；1.0.0 kwargs 传字段。
        source: dict[str, Any] = args[0] if args else event
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "event": "tool_call",
            **source,
            "client": client_name.get(),
        }
        params = payload.get("params")
        if isinstance(params, dict):
            trimmed = {
                key: (
                    value[: self.sql_trunc]
                    if isinstance(value, str) and key == "query"
                    else value[:200] if isinstance(value, str) else value
                )
                for key, value in params.items()
            }
            masker = getattr(self, "masking", None) or getattr(self, "_masking", None)
            if masker is not None:
                trimmed = masker.mask(trimmed)
            payload["params"] = trimmed
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        sys.stderr.write(line + "\n")
        if self.file_path:
            with Path(self.file_path).open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
