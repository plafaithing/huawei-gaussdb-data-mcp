"""A/ORA 兼容专项 live 测试：在 DBCOMPATIBILITY='A' 的 GaussDB 库上端到端验证。

运行方式（两种形态共用本文件，结果对照写进 README §4a）：

- 本地集中式容器（n8n_test，A 库，开源 psycopg2-binary）：

    cd gaussdb-data-mcp
    GAUSSDB_DATA_MCP_ACOMPAT_CREDENTIALS_FILE=../ops/local-acompat-credentials.json \
      uv run --group local pytest tests/integration/test_live_acompat.py -m live -v

- 云上分布式（GY1，先由 run_live_acompat.sh 建出 A 库并注入凭证）：

    bash tests/integration/run_live_acompat.sh

凭证由环境变量 GAUSSDB_DATA_MCP_ACOMPAT_CREDENTIALS_FILE 注入（指向 A 库的
credentials.json）；未设置时整个文件按 marker=live 跳过，本机 ``uv run pytest``
基线不受影响。覆盖点（survey/04 §7 A 兼容盲区）：

- DUAL 伪表、空串即 NULL（A 语义）、层次查询 START WITH ... CONNECT BY（词法层
  已修复 START 误拒）、MERGE INTO 往返、PG 式 ON CONFLICT 在 A 库的数据库报错
  → EXEC_FAILED 信封（错误映射路径）。
- Oracle 语法深化轮（2026-10-12）：MINUS/INTERSECT 集合运算符、DECODE、
  TO_CHAR+SYSDATE、优化器提示注释；序列三层兜底结论——词法层放行
  （seq.NEXTVAL / nextval() / setval() 均归 READ），第二层只读会话由内核拦截
  （ReadOnlySqlTransaction，实测 507），CURRVAL 会话内未 NEXTVAL 时报 currval
  undefined；写通道 INSERT ... VALUES (seq.NEXTVAL) 不受影响（合法 DML）。

需要 DDL 的用例先走 execute_sql_dangerous 建探测表；账号无 DDL 权限时（如本地
appuser）该用例 skip 并把数据库原文报错留在 skip reason 里。
"""

import json
import os
import re

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("GAUSSDB_DATA_MCP_ACOMPAT_CREDENTIALS_FILE"),
        reason="GAUSSDB_DATA_MCP_ACOMPAT_CREDENTIALS_FILE is not set; "
        "acompat tests need an A/ORA-compat database.",
    ),
]

_HIER = "_mcp_acompat_hier"
_TARGET = "_mcp_acompat_target"
_SOURCE = "_mcp_acompat_source"
_UPSERT = "_mcp_acompat_upsert"
_SEQ = "_mcp_acompat_seq"
_MINUS_T1 = "_mcp_acompat_minus_t1"
_MINUS_T2 = "_mcp_acompat_minus_t2"


async def _call(server, tool: str, args: dict) -> dict:
    """in-process 全链路调用：mcp.Client → 中间件 → 工具体 → 真库。"""
    from mcp import Client

    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


async def _ddl(server, sql: str) -> dict:
    """DDL 通道执行（confirm 已过确认门；ENABLE_DDL 由调用方 monkeypatch）。"""
    return await _call(server, "execute_sql_dangerous", {"query": sql, "confirm": True})


async def _require_a_compat(server) -> None:
    """目标库必须是 Oracle 兼容（DBCOMPATIBILITY），否则 skip 并记录实际模式。

    两形态的 datcompatibility 表示不同（实测 507）：集中式显示短名 'A'，
    分布式显示全称 'ORA'——两种都算 A 兼容。分布式若拒绝 A 兼容建库
    （GY1 实测 'A' 被拒：InvalidParameterValue: Compatibility args A is
    invalid），探测库以其他模式建成时本文件的 A 语义断言全部跳过，
    真实模式留在 skip reason 里。
    """
    payload = await _call(
        server,
        "execute_sql",
        {"query": ("SELECT datcompatibility FROM pg_database WHERE datname = current_database()")},
    )
    mode = payload["rows"][0][0]
    if mode not in ("A", "ORA"):
        pytest.skip(
            f"target database is DBCOMPATIBILITY='{mode}', not A/ORA; "
            "A-semantics assertions (DUAL / empty-string-as-NULL / ...) do not apply."
        )


async def _create_or_skip(server, table: str, ddl_body: str) -> None:
    """清残留后建探测表；失败（权限/形态限制）则 skip 并保留数据库原文。"""
    await _ddl(server, f"DROP TABLE IF EXISTS {table}")
    made = await _ddl(server, f"CREATE TABLE {table} {ddl_body}")
    if made.get("error"):
        pytest.skip(f"DDL unavailable on this account: {made['error'].get('message', '')[:180]}")


class TestACompatRead:
    """纯读形态（READ_ONLY 默认 true）：DUAL 与空串即 NULL 的 A 语义。"""

    @pytest.fixture
    def server(self, monkeypatch):
        from gaussdb_data_mcp.server import build_sql_server

        monkeypatch.setenv(
            "GAUSSDB_DATA_MCP_CREDENTIALS_FILE",
            os.environ["GAUSSDB_DATA_MCP_ACOMPAT_CREDENTIALS_FILE"],
        )
        return build_sql_server()

    @pytest.fixture(autouse=True)
    async def _a_compat_guard(self, server):
        await _require_a_compat(server)

    async def test_select_from_dual(self, server):
        payload = await _call(server, "execute_sql", {"query": "SELECT 'x' AS v FROM DUAL"})
        assert payload == {
            "columns": ["v"],
            "row_count": 1,
            "rows": [["x"]],
            "truncated": False,
        }

    async def test_empty_string_is_null(self, server):
        # A 兼容语义：'' 即 NULL（PG 语义下是空串）。两个断言分别锁行为面：
        # '' IS NULL 为真、SELECT '' 字面量返回 null 而非空串。
        payload = await _call(
            server, "execute_sql", {"query": "SELECT '' IS NULL AS empty_is_null"}
        )
        assert payload["rows"] == [["True"]]

        literal = await _call(server, "execute_sql", {"query": "SELECT '' AS empty_literal"})
        assert literal["rows"] == [[None]]


class TestACompatDml:
    """需要 DDL/写通道的 A 兼容行为（READ_ONLY=false + ENABLE_DDL=true）。"""

    @pytest.fixture
    def server(self, monkeypatch):
        from gaussdb_data_mcp.server import build_sql_server

        monkeypatch.setenv(
            "GAUSSDB_DATA_MCP_CREDENTIALS_FILE",
            os.environ["GAUSSDB_DATA_MCP_ACOMPAT_CREDENTIALS_FILE"],
        )
        monkeypatch.setenv("GAUSSDB_DATA_MCP_READ_ONLY", "false")
        monkeypatch.setenv("GAUSSDB_DATA_MCP_ENABLE_DDL", "true")
        return build_sql_server()

    @pytest.fixture(autouse=True)
    async def _a_compat_guard(self, server):
        await _require_a_compat(server)

    async def test_hierarchical_query(self, server):
        # 层次查询：词法层曾误拒（START 在 UTILITY 扫描名单）——本用例端到端锁定
        # SELECT ... START WITH ... CONNECT BY 在 A 库的真实可执行性。
        await _create_or_skip(server, _HIER, "(id integer, parent_id integer)")
        try:
            inserted = await _call(
                server,
                "execute_sql_write",
                {
                    "query": (
                        f"INSERT INTO {_HIER} (id, parent_id) "
                        "VALUES (1, NULL), (2, 1), (3, 1), (4, 2)"
                    )
                },
            )
            assert inserted == {"statement_kind": "INSERT", "rows_affected": 4}

            payload = await _call(
                server,
                "execute_sql",
                {
                    "query": (
                        f"SELECT id FROM {_HIER} "
                        "START WITH id = 1 CONNECT BY PRIOR id = parent_id ORDER BY id"
                    )
                },
            )
            # 根 1 → 子 2、3 → 2 的子 4：层级展开 1,2,3,4
            assert payload["columns"] == ["id"]
            assert payload["rows"] == [["1"], ["2"], ["3"], ["4"]]
        finally:
            await _ddl(server, f"DROP TABLE IF EXISTS {_HIER}")

    async def test_merge_roundtrip(self, server):
        # A 兼容 upsert 的正道：MERGE INTO（文档口径：A 模式不支持 ON CONFLICT）
        await _create_or_skip(server, _TARGET, "(id integer, val text)")
        await _create_or_skip(server, _SOURCE, "(id integer, val text)")
        try:
            await _call(
                server,
                "execute_sql_write",
                {"query": f"INSERT INTO {_TARGET} (id, val) VALUES (1, 'old')"},
            )
            await _call(
                server,
                "execute_sql_write",
                {"query": (f"INSERT INTO {_SOURCE} (id, val) VALUES (1, 'new'), (2, 'fresh')")},
            )

            merged = await _call(
                server,
                "execute_sql_write",
                {
                    "query": (
                        f"MERGE INTO {_TARGET} t USING {_SOURCE} s ON (t.id = s.id) "
                        "WHEN MATCHED THEN UPDATE SET t.val = s.val "
                        "WHEN NOT MATCHED THEN INSERT (id, val) VALUES (s.id, s.val)"
                    )
                },
            )
            assert merged["statement_kind"] == "MERGE"
            assert isinstance(merged["rows_affected"], int)

            payload = await _call(
                server,
                "execute_sql",
                {"query": f"SELECT id, val FROM {_TARGET} ORDER BY id"},
            )
            # matched 行被更新、not-matched 行被插入
            assert payload["rows"] == [["1", "new"], ["2", "fresh"]]
        finally:
            await _ddl(server, f"DROP TABLE IF EXISTS {_TARGET}")
            await _ddl(server, f"DROP TABLE IF EXISTS {_SOURCE}")

    async def test_on_conflict_maps_to_exec_failed(self, server):
        # PG 式 upsert 在 A 库应被数据库拒绝 → EXEC_FAILED 信封（错误映射路径）
        await _create_or_skip(server, _UPSERT, "(id integer PRIMARY KEY, val text)")
        try:
            await _call(
                server,
                "execute_sql_write",
                {"query": f"INSERT INTO {_UPSERT} (id, val) VALUES (1, 'a')"},
            )
            payload = await _call(
                server,
                "execute_sql_write",
                {
                    "query": (
                        f"INSERT INTO {_UPSERT} (id, val) VALUES (1, 'b') "
                        "ON CONFLICT (id) DO NOTHING"
                    )
                },
            )
            assert payload["error"]["code"] == "GAUSSDB_DATA_MCP_EXEC_FAILED"
            # 数据库原文进 message（含数据库错误细节），是写给用户排查的关键信息
            assert payload["error"]["message"]
        finally:
            await _ddl(server, f"DROP TABLE IF EXISTS {_UPSERT}")


class TestOracleSequences:
    """序列三层兜底结论的 live 锁定（词法层放行 → 只读会话拦截，507 实测）。

    - CREATE/DROP SEQUENCE 走 DANGEROUS 通道（确认门生效）；
    - seq.NEXTVAL（属性形态，内核改写为 nextval()）/ nextval() / setval() 过
      execute_sql：内核在只读会话下拒绝（ReadOnlySqlTransaction）→ EXEC_FAILED；
    - CURRVAL 只读会话放行，但本服务读池连接上不可能有成功的 NEXTVAL，故总是
      数据库报 currval undefined（ObjectNotInPrerequisiteState）；
    - 写通道 INSERT ... VALUES (seq.NEXTVAL) 是合法 DML，不受此影响（单测锁定）。
    """

    @pytest.fixture
    def server(self, monkeypatch):
        from gaussdb_data_mcp.server import build_sql_server

        monkeypatch.setenv(
            "GAUSSDB_DATA_MCP_CREDENTIALS_FILE",
            os.environ["GAUSSDB_DATA_MCP_ACOMPAT_CREDENTIALS_FILE"],
        )
        monkeypatch.setenv("GAUSSDB_DATA_MCP_READ_ONLY", "false")
        monkeypatch.setenv("GAUSSDB_DATA_MCP_ENABLE_DDL", "true")
        return build_sql_server()

    @pytest.fixture(autouse=True)
    async def _a_compat_guard(self, server):
        await _require_a_compat(server)

    async def test_sequence_advancement_blocked_by_readonly_session(self, server):
        gate = await _call(
            server, "execute_sql_dangerous", {"query": f"CREATE SEQUENCE {_SEQ}"}
        )
        assert gate.get("confirmation_required") is True  # DDL 确认门对序列同样生效
        made = await _ddl(server, f"CREATE SEQUENCE {_SEQ} START WITH 1")
        if made.get("error"):
            pytest.skip(
                f"CREATE SEQUENCE unavailable on this account: "
                f"{made['error'].get('message', '')[:180]}"
            )
        try:
            for sql in (
                f"SELECT {_SEQ}.NEXTVAL FROM DUAL",   # Oracle 属性形态
                f"SELECT nextval('{_SEQ}')",          # PG 函数形态
                f"SELECT setval('{_SEQ}', 1)",
            ):
                payload = await _call(server, "execute_sql", {"query": sql})
                assert payload["error"]["code"] == "GAUSSDB_DATA_MCP_EXEC_FAILED", sql
                # 第二层证据：内核原文 ReadOnlySqlTransaction（507 集中式 A 库实测）
                assert "read-only transaction" in payload["error"]["message"], sql

            currval = await _call(
                server, "execute_sql", {"query": f"SELECT {_SEQ}.CURRVAL FROM DUAL"}
            )
            assert currval["error"]["code"] == "GAUSSDB_DATA_MCP_EXEC_FAILED"
            # CURRVAL 未被只读会话拦（本就只读），报的是会话内 currval undefined
            assert "currval" in currval["error"]["message"].lower()
        finally:
            await _ddl(server, f"DROP SEQUENCE {_SEQ}")


class TestOracleSetOperators:
    """Oracle 集合运算符 MINUS / INTERSECT 端到端（A/ORA 库；EXCEPT 为 PG 形态，
    集中式实测同样可用，见 README §4.1）。集合运算结果行序不保证，按集合断言。"""

    @pytest.fixture
    def server(self, monkeypatch):
        from gaussdb_data_mcp.server import build_sql_server

        monkeypatch.setenv(
            "GAUSSDB_DATA_MCP_CREDENTIALS_FILE",
            os.environ["GAUSSDB_DATA_MCP_ACOMPAT_CREDENTIALS_FILE"],
        )
        monkeypatch.setenv("GAUSSDB_DATA_MCP_READ_ONLY", "false")
        monkeypatch.setenv("GAUSSDB_DATA_MCP_ENABLE_DDL", "true")
        return build_sql_server()

    @pytest.fixture(autouse=True)
    async def _a_compat_guard(self, server):
        await _require_a_compat(server)

    async def test_minus_and_intersect(self, server):
        await _create_or_skip(server, _MINUS_T1, "(v integer)")
        await _create_or_skip(server, _MINUS_T2, "(v integer)")
        try:
            await _call(
                server,
                "execute_sql_write",
                {"query": f"INSERT INTO {_MINUS_T1} VALUES (1), (2), (3)"},
            )
            await _call(
                server,
                "execute_sql_write",
                {"query": f"INSERT INTO {_MINUS_T2} VALUES (2), (3), (4)"},
            )

            minus = await _call(
                server,
                "execute_sql",
                {"query": f"SELECT v FROM {_MINUS_T1} MINUS SELECT v FROM {_MINUS_T2}"},
            )
            assert set(tuple(row) for row in minus["rows"]) == {("1",)}

            intersect = await _call(
                server,
                "execute_sql",
                {
                    "query": (
                        f"SELECT v FROM {_MINUS_T1} "
                        f"INTERSECT SELECT v FROM {_MINUS_T2}"
                    )
                },
            )
            assert set(tuple(row) for row in intersect["rows"]) == {("2",), ("3",)}
        finally:
            await _ddl(server, f"DROP TABLE IF EXISTS {_MINUS_T1}")
            await _ddl(server, f"DROP TABLE IF EXISTS {_MINUS_T2}")


class TestOracleReadForms:
    """Oracle 读形态（纯 SELECT、无需建表）：DECODE / TO_CHAR+SYSDATE / 优化器提示注释。"""

    @pytest.fixture
    def server(self, monkeypatch):
        from gaussdb_data_mcp.server import build_sql_server

        monkeypatch.setenv(
            "GAUSSDB_DATA_MCP_CREDENTIALS_FILE",
            os.environ["GAUSSDB_DATA_MCP_ACOMPAT_CREDENTIALS_FILE"],
        )
        return build_sql_server()

    @pytest.fixture(autouse=True)
    async def _a_compat_guard(self, server):
        await _require_a_compat(server)

    async def test_decode(self, server):
        payload = await _call(
            server, "execute_sql", {"query": "SELECT DECODE(2, 1, 'x', 'y') AS d FROM DUAL"}
        )
        assert payload["rows"] == [["y"]]

    async def test_to_char_sysdate(self, server):
        payload = await _call(
            server,
            "execute_sql",
            {"query": "SELECT TO_CHAR(sysdate, 'YYYY-MM-DD') AS d FROM DUAL"},
        )
        # 日期随时间走：断言 YYYY-MM-DD 形态而非具体值
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", payload["rows"][0][0])

    async def test_optimizer_hint_comment(self, server):
        payload = await _call(
            server, "execute_sql", {"query": "SELECT /*+ FULL(t) */ 1 AS v FROM DUAL"}
        )
        assert payload["rows"] == [["1"]]
