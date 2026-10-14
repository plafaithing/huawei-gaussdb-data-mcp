"""审计：继承 core AuditLogger，把 SQL 文本截断从 200 放宽（诊断需要），
并附加 HTTP 客户端身份（auth 中间件写入 contextvar）。

注意：core 的 emit 把 ``value[:200]`` 硬编码在方法体内，子类预截断后调
``super().emit()`` 会被二次截回 200——因此这里完整重写 emit，仅放宽 query
键，其余键保持 core 语义（200 截断 + masking）。core 钉死 1.1.1（本地
wheel），复制这十几行可控。
"""

import json
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from huaweicloud_mcp_core.guardrails.audit import AuditLogger

client_name: ContextVar[str] = ContextVar("gaussdb_data_mcp_client", default="stdio")


class SqlAuditLogger(AuditLogger):
    def __init__(self, file_path: str | None = None, *, sql_trunc: int = 2000) -> None:
        super().__init__(file_path=file_path)
        self.sql_trunc = sql_trunc

    def emit(self, event: dict[str, Any]) -> None:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "event": "tool_call",
            **event,
            "client": client_name.get(),
        }
        params = payload.get("params")
        if isinstance(params, dict):
            payload["params"] = self.masking.mask(
                {
                    key: (
                        value[: self.sql_trunc]
                        if isinstance(value, str) and key == "query"
                        else value[:200] if isinstance(value, str) else value
                    )
                    for key, value in params.items()
                }
            )
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        sys.stderr.write(line + "\n")
        if self.file_path:
            with Path(self.file_path).open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
