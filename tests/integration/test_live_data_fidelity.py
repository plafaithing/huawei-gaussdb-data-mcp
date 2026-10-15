"""数据保真度 live 测试（教训 L8 固化，2026-10-15）：面向中国客户场景的真实数据形态。

此前三套 live 测试数据全是 ASCII（issue #1 用户发现 pyodbc 轨中文乱码才暴露）——
「能连上、能查 apple/pear」不等于「能用」，数据保真是功能的一部分。本文件锁定：

- 中文往返（write+read 通道）：常用词 / 生僻混繁体 / 首尾空格，读回逐字符一致；
- 特殊字符：单引号 / 反斜杠 / 真实换行 / 4 字节 emoji；
- NULL vs 空串：A/ORA 兼容语义（'' 即 NULL，文档化行为，非 A 兼容库 skip）；
- 长文本：5000 字中文全量读回 + max_cell_chars 截断标记（前 N 字符 + …）；
- 数字边界：0 / -1 / int64 上限 / 3.14159 / 0.1，字符串化后精度不丢。

两轨共用本文件（驱动按 GAUSSDB_DATA_MCP_DRIVER 解析，Windows 默认 pyodbc）：

    # psycopg2 轨（本地容器，开源 psycopg2-binary，连本地 A 兼容库）
    GAUSSDB_DATA_MCP_FIDELITY_CREDENTIALS_FILE=../ops/local-acompat-credentials.json \
    GAUSSDB_DATA_MCP_DRIVER=psycopg2 \
      uv run --group local pytest tests/integration/test_live_data_fidelity.py -m live -v

    # pyodbc 轨（Windows 原生 + GaussDB ODBC；把上面的 DRIVER 换成 pyodbc、
    # --group local 换成 --group windows，其余不变）

未设 FIDELITY_CREDENTIALS_FILE 时整个文件按 marker=live 跳过，本机基线不受影响。
探针表 _mcp_fidelity_probe 建删走 execute_sql_dangerous 确认流程，finally 兜底 DROP
（与 test_live.py 同款清理模式）；预探测事实（507.0.0 本地集中式 A 库，2026-10-15）：
standard_conforming_strings=on、server_encoding=UTF8、字面量用 E'' 形态显式化
（不依赖 s_c_s 取值）、A 库空串入库即 NULL。
"""

import json
import os

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("GAUSSDB_DATA_MCP_FIDELITY_CREDENTIALS_FILE"),
        reason="GAUSSDB_DATA_MCP_FIDELITY_CREDENTIALS_FILE is not set; "
        "fidelity tests need a real GaussDB instance.",
    ),
]

_PROBE = "_mcp_fidelity_probe"


async def _call(server, tool: str, args: dict) -> dict:
    """in-process 全链路调用：mcp.Client → 中间件 → 工具体 → 真库。"""
    from mcp import Client

    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


async def _ddl(server, sql: str) -> dict:
    """DDL 通道执行（confirm 已过确认门；ENABLE_DDL 由 fixture 设定）。"""
    return await _call(server, "execute_sql_dangerous", {"query": sql, "confirm": True})


def _sql_literal(value: str) -> str:
    """E'' 形态 SQL 字面量：反斜杠一律按转义语义显式化（E'' 的定义不依赖
    standard_conforming_strings 取值），单引号翻倍，真实换行原样保留在 SQL 文本里。
    这里显式转义是为了让断言只锁「服务 + 驱动」的数据通道，不测 SQL 方言解析。"""
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


async def _require_a_compat(server) -> None:
    """目标库必须是 Oracle 兼容（DBCOMPATIBILITY），否则 skip（真实模式留 skip reason）。

    两形态表示不同（实测 507）：集中式 'A'、分布式 'ORA'。PG 语义库上空串不是
    NULL，A 语义断言不适用——skip 而非失败。
    """
    payload = await _call(
        server,
        "execute_sql",
        {"query": "SELECT datcompatibility FROM pg_database WHERE datname = current_database()"},
    )
    mode = payload["rows"][0][0]
    if mode not in ("A", "ORA"):
        pytest.skip(
            f"target database is DBCOMPATIBILITY='{mode}', not A/ORA; "
            "empty-string-as-NULL assertions do not apply."
        )


class TestTextFidelity:
    """write+read 通道的文本数据保真：INSERT（写）→ SELECT（读）逐字符一致。

    server 固定 READ_ONLY=false + ENABLE_DDL=true：探针表建删走 DANGEROUS 通道
    确认流程，数据行走 write 通道，读回走 read 通道（三条通道都过真库）。
    """

    @pytest.fixture
    def server(self, monkeypatch):
        from gaussdb_data_mcp.server import build_sql_server

        monkeypatch.setenv(
            "GAUSSDB_DATA_MCP_CREDENTIALS_FILE",
            os.environ["GAUSSDB_DATA_MCP_FIDELITY_CREDENTIALS_FILE"],
        )
        monkeypatch.setenv("GAUSSDB_DATA_MCP_READ_ONLY", "false")
        monkeypatch.setenv("GAUSSDB_DATA_MCP_ENABLE_DDL", "true")
        return build_sql_server()

    async def _probe_roundtrip(self, server, values: list[str]) -> list[str | None]:
        """建探针表 → 逐行 INSERT（write 通道）→ SELECT 读回（read 通道）。

        返回读回的 val 列；finally 兜底 DROP 探针表。DDL 被拒（账号无权限）时 skip。
        """
        await _ddl(server, f"DROP TABLE IF EXISTS {_PROBE}")
        made = await _ddl(server, f"CREATE TABLE {_PROBE} (id integer, val text)")
        if made.get("error"):
            pytest.skip(
                f"DDL unavailable on this account: {made['error'].get('message', '')[:180]}"
            )
        try:
            for i, value in enumerate(values, 1):
                query = f"INSERT INTO {_PROBE} (id, val) VALUES ({i}, {_sql_literal(value)})"
                inserted = await _call(server, "execute_sql_write", {"query": query})
                assert inserted == {"statement_kind": "INSERT", "rows_affected": 1}
            payload = await _call(
                server, "execute_sql", {"query": f"SELECT id, val FROM {_PROBE} ORDER BY id"}
            )
            assert payload["columns"] == ["id", "val"]
            assert payload["truncated"] is False  # 行数截断未触发（截断的是单元格）
            return [row[1] for row in payload["rows"]]
        finally:
            await _ddl(server, f"DROP TABLE IF EXISTS {_PROBE}")

    async def test_chinese_roundtrip(self, server):
        # 中文（含生僻混繁体「擊敗」与首尾空格）：read/write 双通道往返逐字符一致
        values = ["张三", "北京上海", "擊敗", " 用户 "]
        assert await self._probe_roundtrip(server, values) == values

    async def test_special_characters(self, server):
        # 单引号 / 反斜杠 / 真实换行 / 4 字节 emoji——SQL 字面量边界与编码链路
        values = ["O'Brien", "反\\斜杠", "换行\n符", "🚀"]
        assert await self._probe_roundtrip(server, values) == values

    async def test_long_text_full_roundtrip(self, server, monkeypatch):
        # 5000 字中文全量读回：max_cell_chars 先拉大，证明 5000 字本体不丢
        text = "".join(chr(0x4E00 + i % 500) for i in range(5000))
        monkeypatch.setenv("GAUSSDB_DATA_MCP_MAX_CELL_CHARS", "20000")
        assert await self._probe_roundtrip(server, [text]) == [text]

    async def test_long_text_cell_truncation_marker(self, server, monkeypatch):
        # max_cell_chars 截断：读回 = 前 N 字符 + …（服务侧 _cell 的截断标记；
        # truncated 字段是行数截断标志，与单元格截断无关）
        text = "".join(chr(0x4E00 + i % 500) for i in range(5000))
        monkeypatch.setenv("GAUSSDB_DATA_MCP_MAX_CELL_CHARS", "64")
        cells = await self._probe_roundtrip(server, [text])
        assert cells == [text[:64] + "…"]

    async def test_null_and_empty_string(self, server):
        # A/ORA 兼容语义（文档化行为，README §4.1）：'' 即 NULL——空串与 NULL 在
        # A 兼容库不可区分。端到端锁死：字面量判断 + 入库读回都是 NULL。
        # 布尔用 CASE 显式字符串化（'' IS NULL 的原生布尔经两轨驱动得到的
        # 字符串化表示不同：psycopg2 'True' / pyodbc '1'——语义断言不依赖它）。
        await _require_a_compat(server)
        payload = await _call(
            server,
            "execute_sql",
            {"query": "SELECT CASE WHEN '' IS NULL THEN 'yes' ELSE 'no' END AS v"},
        )
        assert payload["rows"] == [["yes"]]
        payload = await _call(server, "execute_sql", {"query": "SELECT NULL AS v"})
        assert payload["rows"] == [[None]]

        await _ddl(server, f"DROP TABLE IF EXISTS {_PROBE}")
        created = await _ddl(server, f"CREATE TABLE {_PROBE} (id integer, val text)")
        if created.get("error"):
            pytest.skip(
                f"DDL unavailable on this account: {created['error'].get('message', '')[:180]}"
            )
        try:
            inserted = await _call(
                server,
                "execute_sql_write",
                {"query": f"INSERT INTO {_PROBE} (id, val) VALUES (1, ''), (2, NULL)"},
            )
            assert inserted == {"statement_kind": "INSERT", "rows_affected": 2}
            payload = await _call(
                server, "execute_sql", {"query": f"SELECT id, val FROM {_PROBE} ORDER BY id"}
            )
            # A 语义：'' 入库读回 None，与显式 NULL 不可区分
            assert payload["rows"] == [["1", None], ["2", None]]
        finally:
            await _ddl(server, f"DROP TABLE IF EXISTS {_PROBE}")

    async def test_numeric_boundaries(self, server):
        # 数字边界：0 / -1 / int64 上限 / 小数——服务把值字符串化（_cell），断言
        # 字符串化后精度不丢（bigint 原样、numeric 精确到给定小数位）
        await _ddl(server, f"DROP TABLE IF EXISTS {_PROBE}")
        made = await _ddl(server, f"CREATE TABLE {_PROBE} (id integer, bi bigint, num numeric)")
        if made.get("error"):
            pytest.skip(
                f"DDL unavailable on this account: {made['error'].get('message', '')[:180]}"
            )
        try:
            cases = [
                (1, "0", "3.14159"),
                (2, "-1", "0.1"),
                (3, "9223372036854775807", "3.14159"),
            ]
            for i, bi, num in cases:
                inserted = await _call(
                    server,
                    "execute_sql_write",
                    {"query": f"INSERT INTO {_PROBE} (id, bi, num) VALUES ({i}, {bi}, {num})"},
                )
                assert inserted == {"statement_kind": "INSERT", "rows_affected": 1}
            payload = await _call(
                server,
                "execute_sql",
                {"query": f"SELECT id, bi, num FROM {_PROBE} ORDER BY id"},
            )
            assert payload["rows"] == [[str(i), bi, num] for i, bi, num in cases]
        finally:
            await _ddl(server, f"DROP TABLE IF EXISTS {_PROBE}")
