"""HTTP 传输的 Bearer 鉴权中间件（core 没有的能力——两个 example 都在 0.0.0.0 裸奔）。

- tokens 文件每次请求热读（与凭证同款轮换语义）。
- 常数时间比较（bytes 形式：str 版 compare_digest 遇非 ASCII 会抛 TypeError）；
  失败一律 401 + WWW-Authenticate。
- tokens 文件不可用（缺失/损坏，如原子替换的中间态）→ 503 + Retry-After: 5，
  而非异常冒泡成 500；scheme 大小写不敏感（RFC 7235）。
- stdio 模式不经过本中间件（本地信任边界）。
"""

import hmac

from huaweicloud_mcp_core.errors import McpToolError

from .audit import client_name
from .credentials import load_tokens

_WWW_AUTH = (b"www-authenticate", b'Bearer realm="gaussdb-data-mcp"')
_RETRY_AFTER = (b"retry-after", b"5")


class BearerAuthMiddleware:
    def __init__(self, app, *, tokens_file: str) -> None:
        self.app = app
        self.tokens_file = tokens_file

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = next(
            (value for key, value in scope.get("headers", []) if key == b"authorization"), b""
        )
        # RFC 7235：认证 scheme 大小写不敏感（bearer 与 Bearer 等价）
        scheme, _, value = header.decode("latin-1").partition(" ")
        token = value.strip().encode("utf-8") if scheme.lower() == "bearer" else b""
        try:
            tokens = load_tokens(self.tokens_file)
        except McpToolError:
            await send(
                {"type": "http.response.start", "status": 503,
                 "headers": [_RETRY_AFTER, (b"content-length", b"0")]}
            )
            await send({"type": "http.response.body", "body": b""})
            return
        matched = next(
            (
                name
                for name, value in tokens.items()
                if hmac.compare_digest(value.encode("utf-8"), token)
            ),
            None,
        )
        if matched is None:
            await send(
                {"type": "http.response.start", "status": 401,
                 "headers": [_WWW_AUTH, (b"content-length", b"0")]}
            )
            await send({"type": "http.response.body", "body": b""})
            return
        client_name.set(matched)
        await self.app(scope, receive, send)
