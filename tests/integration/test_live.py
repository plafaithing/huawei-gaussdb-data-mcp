# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: MIT

"""真库 live 集成测试：在真实华为云 GaussDB 实例上端到端验证全部 7 个工具。

运行方式（宿主机仓库根，详见同目录 README.md）：

    bash server/mcp_server_gaussdb_data/tests/integration/run_live.sh

官方驱动仅 Linux/py311，宿主 Windows 无法直跑——runner 会在 python:3.11-slim
容器内安装驱动与项目后执行本文件。凭证由 runner 注入：
GAUSSDB_DATA_MCP_CREDENTIALS_FILE 指向 0600 的 JSON（host/port/dbname/user/
password/sslmode=require）。未设置时整个文件按 marker=live 跳过，本机
``uv run pytest`` 基线不受影响。

写通道测试会在目标库 public schema 建立并删除表 _mcp_live_probe（专用测试
实例，可接受）；失败路径 finally 里尽力 DROP IF EXISTS 清理。
"""

import json
import os

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("GAUSSDB_DATA_MCP_CREDENTIALS_FILE"),
        reason="GAUSSDB_DATA_MCP_CREDENTIALS_FILE is not set; live tests need a real instance.",
    ),
]

_PROBE_TABLE = "_mcp_live_probe"


async def _call(server, tool: str, args: dict) -> dict:
    """in-process 全链路调用：mcp.Client → 中间件 → 工具体 → 真库。"""
    from mcp import Client

    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


def _build_read_only_server():
    from mcp_server_gaussdb_data.server import build_sql_server

    return build_sql_server()  # READ_ONLY 默认 true（build 时快照进中间件）


class TestReadOnlyDefault:
    """默认 READ_ONLY=true：读通道真库全绿，写工具被中间件拦截。"""

    @pytest.fixture
    def server(self):
        return _build_read_only_server()

    async def test_select_roundtrip(self, server):
        payload = await _call(server, "execute_sql", {"query": "SELECT 1 AS v"})
        assert payload == {
            "columns": ["v"],
            "row_count": 1,
            "rows": [["1"]],
            "truncated": False,
        }

    async def test_list_schemas_real(self, server):
        payload = await _call(server, "list_schemas", {})
        assert payload["columns"] == ["schema_name"]
        schemas = {row[0] for row in payload["rows"]}
        # survey 备案项（实测 507.0.0）：schemata 返回系统 schema（dbe_* / pkg_* /
        # rds* / sqladvisor 等）+ information_schema + pg_catalog + public；
        # 并不存在名为 postgres 的 schema。
        assert {"public", "information_schema", "pg_catalog"} <= schemas
        # $user schema（search_path 首位，与登录账号同名）存在时必须被列出：
        # 云上初始账号 root 的同名 schema 存在（507 实测）；本地容器轨用最小权限
        # 业务账号，可能无同名 schema——存在性以 pg_namespace 前置查询为准
        # （count 字符串化 "0"/"1" 两轨一致，布尔则 psycopg2 'True' / pyodbc '1'）。
        mine = await _call(
            server,
            "execute_sql",
            {
                "query": (
                    "SELECT current_user, "
                    "(SELECT count(*) FROM pg_namespace WHERE nspname = current_user)"
                )
            },
        )
        user, own_schema_count = mine["rows"][0]
        if own_schema_count != "0":
            assert user in schemas

    async def test_list_tables_real(self, server):
        payload = await _call(server, "list_tables", {"schema": "public"})
        assert payload["columns"] == ["table_name", "table_type"]
        assert isinstance(payload["rows"], list)
        assert payload["truncated"] is False

    async def test_describe_table_real(self, server):
        payload = await _call(
            server, "describe_table", {"schema": "information_schema", "table": "tables"}
        )
        assert payload["columns"] == [
            "column_name",
            "data_type",
            "is_nullable",
            "column_default",
        ]
        names = {row[0] for row in payload["rows"]}
        assert "table_name" in names

    async def test_explain_real(self, server):
        payload = await _call(server, "explain_sql", {"query": "SELECT 1"})
        # EXPLAIN 输出形态随驱动与部署而异（2026-10 云上 E2E 回归 + 本地容器实测），
        # 已观测三种形态，断言只锁共同契约（有列、有行、首行数据非空、无行截断），
        # 不假设具体列名/列数——列名是驱动的产物，不是本服务的契约：
        # - psycopg2 轨（本地容器 507.0.0）：单列 "====== Query Others ====="
        #   （GaussDB 风格），计划行如 "Parameterize: Yes, parameterized sql: ..."；
        # - pyodbc 轨 gsqlodbc（本地容器 507.0.0）：单列 "QUERY PLAN"（PG 风格）；
        # - pyodbc 轨 gsqlodbc（云实例）：结构化 5 列 id/operation/E-rows/E-width/E-costs。
        assert payload["columns"], payload["columns"]
        assert payload["row_count"] >= 1
        assert payload["rows"][0][0]
        assert payload["truncated"] is False

    async def test_read_channel_rejects_write(self, server):
        payload = await _call(server, "execute_sql", {"query": "DELETE FROM _no_such_table"})
        assert payload["error"]["code"] == "GAUSSDB_DATA_MCP_RISK_MISMATCH"

    async def test_read_only_blocks_write_tool(self, server):
        payload = await _call(server, "execute_sql_write", {"query": "SELECT 1"})
        assert payload["error"]["code"] == "MCP_READ_ONLY"
        assert "GAUSSDB_DATA_MCP_READ_ONLY" in payload["error"]["hint"]


class TestWriteChannels:
    """READ_ONLY=false 重建 server（build 时快照）：写/DDL 通道真建删表。

    execute_sql_dangerous 是 DANGEROUS 风险：confirm 缺省时被 core 中间件的
    确认门拦截（confirmation_required 信封），confirm=true 才到达工具体，
    工具体再校验 GAUSSDB_DATA_MCP_ENABLE_DDL（每调用热读 env）。
    """

    @pytest.fixture
    def server(self, monkeypatch):
        from mcp_server_gaussdb_data.server import build_sql_server

        monkeypatch.setenv("GAUSSDB_DATA_MCP_READ_ONLY", "false")
        return build_sql_server()

    async def test_dml_roundtrip(self, server, monkeypatch):
        monkeypatch.setenv("GAUSSDB_DATA_MCP_ENABLE_DDL", "true")
        create = f"CREATE TABLE {_PROBE_TABLE} (id integer, label text)"
        drop = f"DROP TABLE IF EXISTS {_PROBE_TABLE}"

        # 1) DANGEROUS 无 confirm → confirmation_required 信封（表尚未建）
        first = await _call(server, "execute_sql_dangerous", {"query": create})
        try:
            # 断言必须在 try/finally 保护内：确认门若回归失效，上一步会直接建表
            # 成功而本断言失败——无保护时清理被跳过，探针表泄漏进目标库
            # （2026-10 云上 E2E 实测，_mcp_live_probe 泄漏后手动 DROP）
            assert first["confirmation_required"] is True

            # 2) confirm=true → 建表成功（DDL rowcount 因驱动而异，只断言形状）
            made = await _call(server, "execute_sql_dangerous", {"query": create, "confirm": True})
            assert made["statement_kind"] == "CREATE"
            assert isinstance(made["rows_affected"], int)

            # 3) write 通道 INSERT → rows_affected=1
            inserted = await _call(
                server,
                "execute_sql_write",
                {"query": f"INSERT INTO {_PROBE_TABLE} (id, label) VALUES (1, 'live')"},
            )
            assert inserted == {"statement_kind": "INSERT", "rows_affected": 1}

            # 4) read 通道 SELECT 验证行（值经 _cell 字符串化）
            selected = await _call(
                server, "execute_sql", {"query": f"SELECT id, label FROM {_PROBE_TABLE}"}
            )
            assert selected["columns"] == ["id", "label"]
            assert selected["rows"] == [["1", "live"]]
        finally:
            dropped = await _call(server, "execute_sql_dangerous", {"query": drop, "confirm": True})
            assert dropped["statement_kind"] == "DROP"

    async def test_ddl_disabled_without_switch(self, server, monkeypatch):
        monkeypatch.setenv("GAUSSDB_DATA_MCP_ENABLE_DDL", "false")
        # confirm=true 越过中间件确认门，工具体内的 ENABLE_DDL 检查才生效
        payload = await _call(
            server,
            "execute_sql_dangerous",
            {"query": f"DROP TABLE IF EXISTS {_PROBE_TABLE}", "confirm": True},
        )
        assert payload["error"]["code"] == "GAUSSDB_DATA_MCP_DDL_DISABLED"


class TestAudit:
    """审计落盘：GAUSSDB_DATA_MCP_AUDIT_FILE 指向临时文件，调一次工具后核对 JSONL。"""

    async def test_audit_file_written(self, tmp_path, monkeypatch):
        from mcp_server_gaussdb_data.server import build_sql_server

        audit = tmp_path / "audit.jsonl"
        monkeypatch.setenv("GAUSSDB_DATA_MCP_AUDIT_FILE", str(audit))
        server = build_sql_server()

        payload = await _call(server, "execute_sql", {"query": "SELECT 1 AS v"})
        assert payload["rows"] == [["1"]]  # 先证明真触库，再看审计

        lines = audit.read_text(encoding="utf-8").splitlines()
        assert lines, "audit file must contain at least one JSONL record"
        event = json.loads(lines[-1])
        assert event["tool"] == "execute_sql"
        assert event["risk"] == "READ"
        assert event["decision"] == "allowed"
        assert event["params"]["query"] == "SELECT 1 AS v"
        assert event["client"] == "stdio"  # in-process 客户端无 auth 中间件
