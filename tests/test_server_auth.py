import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from huaweicloud_mcp_core.errors import McpToolError

from gaussdb_data_mcp.auth import BearerAuthMiddleware
from gaussdb_data_mcp.credentials import load_tokens


class _DummyApp:
    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _write_tokens(tmp_path: Path, tokens: dict) -> str:
    f = tmp_path / "tokens.json"
    f.write_text(json.dumps({"tokens": tokens}), encoding="utf-8")
    return str(f)


async def _call(app, token: str | None, *, scheme: str = "Bearer"):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    headers = [(b"authorization", f"{scheme} {token}".encode())] if token else []
    await app({"type": "http", "method": "POST", "path": "/mcp", "headers": headers},
              receive, send)
    return messages


async def test_valid_token_passes(tmp_path):
    dummy = _DummyApp()
    app = BearerAuthMiddleware(dummy, tokens_file=_write_tokens(tmp_path, {"dify": "tok_1"}))
    messages = await _call(app, "tok_1")
    assert messages[0]["status"] == 200 and dummy.calls == 1


async def test_missing_or_bad_token_401(tmp_path):
    dummy = _DummyApp()
    app = BearerAuthMiddleware(dummy, tokens_file=_write_tokens(tmp_path, {"dify": "tok_1"}))
    assert (await _call(app, None))[0]["status"] == 401
    assert (await _call(app, "wrong"))[0]["status"] == 401
    assert dummy.calls == 0


async def test_token_hot_reload(tmp_path):
    path = _write_tokens(tmp_path, {"dify": "tok_1"})
    dummy = _DummyApp()
    app = BearerAuthMiddleware(dummy, tokens_file=path)
    assert (await _call(app, "tok_1"))[0]["status"] == 200
    _write_tokens(tmp_path, {"dify": "tok_2"})
    assert (await _call(app, "tok_1"))[0]["status"] == 401   # 旧 token 立即失效
    assert (await _call(app, "tok_2"))[0]["status"] == 200


async def test_401_has_www_authenticate(tmp_path):
    dummy = _DummyApp()
    app = BearerAuthMiddleware(dummy, tokens_file=_write_tokens(tmp_path, {"dify": "tok_1"}))
    start = (await _call(app, "wrong"))[0]
    assert start["status"] == 401
    assert (b"www-authenticate", b'Bearer realm="gaussdb-data-mcp"') in start["headers"]


async def test_non_ascii_token_fails_closed(tmp_path):
    # 非 ASCII 凭证必须 401 而不是让 compare_digest 抛 TypeError 变 500
    dummy = _DummyApp()
    app = BearerAuthMiddleware(dummy, tokens_file=_write_tokens(tmp_path, {"dify": "tok_1"}))
    assert (await _call(app, "caf\xe9"))[0]["status"] == 401
    assert dummy.calls == 0


async def test_non_http_scope_passes_through(tmp_path):
    # 非 http scope（如 lifespan）直通
    dummy = _DummyApp()
    app = BearerAuthMiddleware(dummy, tokens_file=_write_tokens(tmp_path, {"dify": "tok_1"}))

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        pass

    await app({"type": "lifespan"}, receive, send)
    assert dummy.calls == 1


def test_audit_query_truncation_only_for_query_key(tmp_path):
    from gaussdb_data_mcp.audit import SqlAuditLogger

    log = tmp_path / "audit.jsonl"
    logger = SqlAuditLogger(file_path=str(log))
    logger.emit({"tool": "sql_query", "params": {"query": "S" * 2500, "other": "x" * 300}})
    event = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert len(event["params"]["query"]) == 2000   # query 键放宽到 sql_trunc
    assert len(event["params"]["other"]) == 200    # 其余键保持 core 的 200
    assert event["client"] == "stdio"              # contextvar 默认值


async def test_audit_client_name_from_contextvar(tmp_path):
    from gaussdb_data_mcp.audit import SqlAuditLogger, client_name

    log = tmp_path / "audit.jsonl"
    logger = SqlAuditLogger(file_path=str(log))
    token = client_name.set("dify")
    try:
        logger.emit({"tool": "sql_query", "params": {"query": "SELECT 1"}})
    finally:
        client_name.reset(token)
    event = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert event["client"] == "dify"


def test_build_sql_server_wiring():
    from gaussdb_data_mcp.server import build_sql_server
    server = build_sql_server()
    assert server.name == "gaussdb-data"


async def test_read_only_block_hint_names_project_env_var(monkeypatch):
    # core 1.1.1 只读拦截的 hint 硬编码 HUAWEICLOUD_MCP_READ_ONLY（本项目无此变量）；
    # SqlGuardrailsMiddleware 覆写指向 GAUSSDB_DATA_MCP_READ_ONLY 的文案（Task 7 审查 Important）。
    # 走 in-process Client 全链路：同时验证 build_sql_server 装配的是覆写子类。
    from mcp import Client

    from gaussdb_data_mcp.server import build_sql_server

    monkeypatch.setenv("GAUSSDB_DATA_MCP_READ_ONLY", "true")
    async with Client(build_sql_server()) as client:
        result = await client.call_tool(
            "execute_sql_write", {"query": "DELETE FROM t WHERE id = 1"}
        )
    envelope = json.loads(result.content[0].text)["error"]
    assert envelope["code"] == "MCP_READ_ONLY"
    assert "GAUSSDB_DATA_MCP_READ_ONLY" in envelope["hint"]
    assert "HUAWEICLOUD" not in envelope["hint"]


# ---- Task 10 硬化项（A1 __main__ guard / A2 auth 硬化 / A3 core 版本断言） ----


def test_python_dash_m_entry_point_prints_help():
    # Task 9 实测：无 __main__ guard 时 `python -m gaussdb_data_mcp.server` 静默退出。
    proc = subprocess.run(
        [sys.executable, "-m", "gaussdb_data_mcp.server", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0
    assert "transport" in (proc.stdout + proc.stderr)


async def test_tokens_file_missing_returns_503_with_retry_after(tmp_path):
    # tokens 文件不可用（被删/原子替换中间态）：503 + Retry-After，而非异常冒泡成 500
    dummy = _DummyApp()
    app = BearerAuthMiddleware(dummy, tokens_file=str(tmp_path / "missing.json"))
    start = (await _call(app, "tok_1"))[0]
    assert start["status"] == 503
    assert (b"retry-after", b"5") in start["headers"]
    assert dummy.calls == 0


async def test_tokens_file_corrupt_returns_503(tmp_path):
    f = tmp_path / "tokens.json"
    f.write_text("{not json", encoding="utf-8")
    dummy = _DummyApp()
    app = BearerAuthMiddleware(dummy, tokens_file=str(f))
    assert (await _call(app, "tok_1"))[0]["status"] == 503
    assert dummy.calls == 0


async def test_bearer_scheme_case_insensitive(tmp_path):
    # RFC 7235：认证 scheme 大小写不敏感（bearer / Bearer / BEARER 等价）
    dummy = _DummyApp()
    app = BearerAuthMiddleware(dummy, tokens_file=_write_tokens(tmp_path, {"dify": "tok_1"}))
    for scheme in ("bearer", "BEARER"):
        assert (await _call(app, "tok_1", scheme=scheme))[0]["status"] == 200
    assert dummy.calls == 2


async def test_non_bearer_scheme_is_401(tmp_path):
    # 非 Bearer scheme（如 Basic）不视为有效凭证
    dummy = _DummyApp()
    app = BearerAuthMiddleware(dummy, tokens_file=_write_tokens(tmp_path, {"dify": "tok_1"}))
    assert (await _call(app, "tok_1", scheme="Basic"))[0]["status"] == 401
    assert dummy.calls == 0


def test_load_tokens_rejects_non_ascii_or_control(tmp_path):
    # token 值仅支持 ASCII 可打印字符：加载层 fail-fast（Task 10 A2）
    for bad in ("tok_\xe9", "tok\x00", "tok\nx"):
        f = tmp_path / "tokens.json"
        f.write_text(json.dumps({"tokens": {"dify": bad}}), encoding="utf-8")
        with pytest.raises(McpToolError) as exc:
            load_tokens(str(f))
        assert exc.value.code == "MCP_CREDENTIAL_ERROR"


def test_load_tokens_accepts_printable_ascii(tmp_path):
    f = tmp_path / "tokens.json"
    f.write_text(json.dumps({"tokens": {"dify": "tok_ABC-xyz_123"}}), encoding="utf-8")
    assert load_tokens(str(f)) == {"dify": "tok_ABC-xyz_123"}


def test_core_version_pinned_for_audit_copy():
    # audit.py 是 core 1.1.1 AuditLogger 的行为副本（query 键截断放宽）。
    # core 升级使本测试变红 = 强制人工复核 audit.py 与新版 core 的对齐（Task 6 审查 M1）。
    assert version("huaweicloud-mcp-core") == "1.1.1"
