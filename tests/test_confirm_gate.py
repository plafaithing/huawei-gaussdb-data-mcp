# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: MIT

"""DANGEROUS confirm 门回归（安全修复 2026-10-23）：门在官方 core 1.0.0 上必须成立。

根因：旧 core 1.1.1 的确认门是「risk is DANGEROUS 或规则 confirm」；官方 core 1.0.0
的确认门只由 guardrail 规则驱动（middleware._pre_check → evaluate_rules），而
load_rules 默认零规则——execute_sql_dangerous 无 confirm 直达工具体（云上 E2E
test_dml_roundtrip 等 3 个 live 用例同根因失败，探针表因此泄漏到云实例）。

修复（不改 core）：build_sql_server 在 load_rules 结果后追加内置规则
gaussdb-data-dangerous-confirm（action=confirm，risks=DANGEROUS），规则门天然
命中危险通道；confirm=true 放行路径与用户 YAML 的加严规则（block > confirm）不变。

测试形态：in-process Client 全链路（Client → 中间件 → 工具体），全程不触真库——
确认门在 call_next 之前裁决；confirm=True 的用例固定 ENABLE_DDL=false，工具体在
触碰凭证/连接池之前即返回 DDL_DISABLED。
"""

import json

import pytest
from huaweicloud_mcp_core import ToolRisk
from huaweicloud_mcp_core.guardrails.rules import Rule
from mcp import Client

from mcp_server_gaussdb_data.server import SqlGuardrailsMiddleware, build_sql_server


async def _call(server, tool: str, args: dict) -> dict:
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


@pytest.fixture
def server(monkeypatch):
    # 只读门在确认门之前裁决（middleware._pre_check 先查 read_only）：放开只读
    # 才能测到 confirm 门；ENABLE_DDL 固定 false——confirm 通过后工具体在触碰
    # 数据库之前即拒绝，测试不依赖真库。
    monkeypatch.setenv("GAUSSDB_DATA_MCP_READ_ONLY", "false")
    monkeypatch.delenv("GAUSSDB_DATA_MCP_ENABLE_DDL", raising=False)
    return build_sql_server()


def test_built_in_dangerous_confirm_rule_installed(server):
    gate = next(m for m in server.middleware if isinstance(m, SqlGuardrailsMiddleware))
    rule = next((r for r in gate.rules if r.id == "gaussdb-data-dangerous-confirm"), None)
    assert isinstance(rule, Rule)
    assert rule.action == "confirm"
    assert rule.risks == (ToolRisk.DANGEROUS,)


async def test_dangerous_without_confirm_returns_confirmation_required(server):
    payload = await _call(server, "execute_sql_dangerous", {"query": "DROP TABLE x"})
    assert payload.get("confirmation_required") is True
    assert "gaussdb-data-dangerous-confirm" in payload["rules_hit"]


async def test_dangerous_with_confirm_reaches_tool_body(server):
    # confirm=true 越过确认门 → 工具体内 ENABLE_DDL 开关检查（连接库之前）→ 该错误
    # 信封本身证明调用已穿过确认门
    payload = await _call(
        server, "execute_sql_dangerous", {"query": "DROP TABLE x", "confirm": True}
    )
    assert payload["error"]["code"] == "GAUSSDB_DATA_MCP_DDL_DISABLED"


async def test_read_channel_not_gated_by_confirm_rule(server):
    # 内置规则只挂 DANGEROUS：READ 通道不得被 confirm 门误伤——无凭证时得到的是
    # 工具体内的凭证错误信封，而非 confirmation_required
    payload = await _call(server, "execute_sql", {"query": "SELECT 1"})
    assert "confirmation_required" not in payload
    assert payload["error"]["code"] == "MCP_CREDENTIAL_ERROR"
