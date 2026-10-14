"""sql_guard 对抗测试套件。所有用例不依赖数据库。"""

from importlib.metadata import version

import pytest
from huaweicloud_mcp_core import ToolRisk
from huaweicloud_mcp_core.errors import McpToolError

from gaussdb_data_mcp.sql_guard import classify_sql


def test_sqlparse_version_pinned():
    # 分类器正确性强耦合 sqlparse 0.6 词法：反引号标识符标为 Name 且值保留
    # 引号、U&"..." 切成 Name+Operator+String 三连、双引号标识符标为 Symbol。
    # pyproject 钉 >=0.6,<0.7；本测试锁定运行时实际版本，变红 = 版本漂移出
    # 约束（如手装 0.5/0.7），须重跑 probe 结论与全部对抗用例。
    assert version("sqlparse").split(".")[:2] == ["0", "6"]


def read(sql: str, **kwargs):
    return classify_sql(sql, **kwargs)


# ---------- 合法 READ ----------
@pytest.mark.parametrize("sql", [
    "SELECT * FROM t",
    "select 1",
    "WITH c AS (SELECT 1) SELECT * FROM c",
    "SHOW max_connections",
    "EXPLAIN SELECT * FROM t",
    "EXPLAIN ANALYZE SELECT * FROM t",
    "VALUES (1), (2)",
    "SELECT 'DROP TABLE x'",            # 字符串字面量不误报
    "SELECT update_time FROM t",        # 标识符不误报
    "-- drop everything\nSELECT 1",     # 注释不误报
    "/* UPDATE all rows */ SELECT 1",
    "SELECT 1;",                        # 尾分号 = 单语句
])
def test_read_ok(sql):
    assert read(sql).risk is ToolRisk.READ


# ---------- 合法 WRITE / DANGEROUS ----------
@pytest.mark.parametrize("sql,kind", [
    ("INSERT INTO t VALUES (1)", "INSERT"),
    ("UPDATE t SET a = 1", "UPDATE"),
    (
        "INSERT INTO t VALUES (1) ON CONFLICT DO UPDATE SET a = 2",
        "INSERT",
    ),  # 子句里的 SET/UPDATE 不拦
    ("WITH c AS (SELECT 1) INSERT INTO t SELECT * FROM c", "WITH"),
    ("CREATE TABLE t (id int)", "CREATE"),
    ("DROP TABLE t", "DROP"),
    ("TRUNCATE TABLE t", "TRUNCATE"),
])
def test_write_ddl_ok(sql, kind):
    assert read(sql).kind == kind


def test_with_write_classified_as_write():
    assert read("WITH c AS (DELETE FROM t RETURNING *) SELECT * FROM c").risk is ToolRisk.WRITE


# ---------- 首关键词拒绝（含 polardb 实测绕过面） ----------
@pytest.mark.parametrize("sql", [
    "SET GLOBAL max_connections = 1000",
    "PREPARE s FROM 'DROP TABLE t'",     # polardb 实测绕过点——这里首关键词即拦
    "EXECUTE s",
    "COPY t FROM STDIN",
    "DO $$ BEGIN DELETE FROM t; END $$",
    "CALL p()",
    "LOCK TABLE t",
    "BEGIN",
    "COMMIT",
    "VACUUM t",
])
def test_first_keyword_rejected(sql):
    with pytest.raises(McpToolError):
        read(sql)


# ---------- 全 token 扫描拒绝（READ 通道内藏写/DDL） ----------
# 注：规则表规定 READ 通道命中扫描关键字即拒绝（不是降级），
# 与 test_read_scan_finds_delete_in_cte 一致，故此处断言抛 McpToolError。
@pytest.mark.parametrize("sql", [
    "SELECT * INTO new_t FROM t",        # SELECT INTO 建表——polardb 未拦，我们必须拦
])
def test_read_channel_scan(sql):
    with pytest.raises(McpToolError):
        read(sql)


def test_read_scan_finds_delete_in_cte():
    # READ 语义语句里藏写关键字同样被扫描拒绝（分类器是纯词法的，不要求语法合法）
    with pytest.raises(McpToolError):
        read("SELECT 1 WHERE 1 = (DELETE FROM t RETURNING 1)")


# ---------- 多语句 / 内容拒绝 ----------
@pytest.mark.parametrize("sql", [
    "SELECT 1; SELECT 2",
    "SELECT 1; DELETE FROM t",
    "SELECT 1\0",
    "",
    "   ",
    ";",
])
def test_invalid_input_rejected(sql):
    with pytest.raises(McpToolError):
        read(sql)


def test_too_long_rejected():
    with pytest.raises(McpToolError):
        read("SELECT '" + "a" * 101 + "' FROM t", max_sql_chars=100)


def test_trailing_garbage_rejected():
    # 尾分号后还有内容 → 多语句
    with pytest.raises(McpToolError):
        read("SELECT 1; -- nothing")


# ---------- 危险函数与敏感会话变量（AWS mysql-mcp 借鉴） ----------
@pytest.mark.parametrize("sql", [
    "SELECT pg_sleep(100)",                       # 读形态调用危险函数
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT * FROM dblink('dbname=x', 'SELECT 1') AS t(c int)",
    "INSERT INTO t VALUES (pg_sleep(1))",         # 写通道同样拦
])
def test_dangerous_function_rejected(sql):
    with pytest.raises(McpToolError):
        read(sql)


def test_dangerous_function_column_name_not_flagged():
    # 同名列（无括号调用）不误报
    assert read("SELECT pg_sleep FROM t").risk is ToolRisk.READ


@pytest.mark.parametrize("sql", [
    "ALTER DATABASE db SET synchronous_commit = off",   # DDL 通道也拒（关完整性）
    "ALTER ROLE r SET row_security = off",
])
def test_sensitive_session_var_rejected_all_channels(sql):
    with pytest.raises(McpToolError):
        read(sql)


# ---------- 自审追加：额外对抗用例（任务自审要求，长期锁定保守行为） ----------
def test_adversarial_comment_between_function_and_paren():
    # 注释插在函数名与 '(' 之间试图绕过危险函数锚定——同样拦截
    with pytest.raises(McpToolError):
        read("SELECT pg_sleep /* bypass */ (100)")


def test_adversarial_leading_comment_not_overblocked():
    # 前导注释不影响正常 READ 放行（不因防御而误拒）
    assert read("/* leading */ SELECT 1").risk is ToolRisk.READ


def test_adversarial_comment_between_statements():
    with pytest.raises(McpToolError):
        read("SELECT 1 /* tail */; SELECT 2")


@pytest.mark.parametrize("sql,risk,kind", [
    ("sElEcT * fRoM t", ToolRisk.READ, "SELECT"),        # 大小写归一：小写放行
    ("drop table t", ToolRisk.DANGEROUS, "DROP"),        # 小写 DDL 仍归 DANGEROUS
    ("With c As (Insert Into t Select 1) Select * From c", ToolRisk.WRITE, "WITH"),
])
def test_adversarial_case_folding(sql, risk, kind):
    result = read(sql)
    assert (result.risk, result.kind) == (risk, kind)


def test_adversarial_mixed_case_sensitive_var_rejected():
    # 大小写混淆的敏感变量（首关键词门 + 敏感变量名双重拦截）
    with pytest.raises(McpToolError):
        read("SeT sEsSiOn_rEpLiCaTiOn_rOlE = replica")


def test_adversarial_cte_hides_ddl():
    # CTE 里藏 DDL → WITH 归 DANGEROUS（Task 7 通道锁按 risk 拦截）
    assert read("WITH x AS (CREATE TABLE y (id int)) SELECT * FROM x").risk is ToolRisk.DANGEROUS


def test_adversarial_nested_function_call():
    # 子查询里嵌套调用危险函数
    with pytest.raises(McpToolError):
        read("SELECT * FROM t WHERE x = (SELECT pg_read_file('a'))")


def test_adversarial_schema_qualified_function():
    # schema 前缀的危险函数同样拦截（词法层锚定 Name + '('）
    with pytest.raises(McpToolError):
        read("SELECT public.pg_sleep(1)")


def test_adversarial_quoted_identifier_function_not_flagged():
    # 引号包裹的同名标识符不是函数调用——不误报
    assert read('SELECT "pg_sleep" FROM t').risk is ToolRisk.READ


def test_adversarial_trailing_block_comment_rejected():
    # 尾分号后的块注释：sqlparse 会切成纯注释 statement，过滤后仅剩 1 条——
    # 仍必须拒绝（分号后还有内容）
    with pytest.raises(McpToolError):
        read("SELECT 1; /* c */")


def test_adversarial_trailing_space_after_semicolon_ok():
    # 尾分号后只有空白 = 单语句，放行
    assert read("SELECT 1; ").risk is ToolRisk.READ


def test_adversarial_for_update_conservatively_rejected():
    # FOR UPDATE 是合法读锁语句，但 UPDATE 命中 READ 扫描 → 保守拒绝（已知误报，锁定行为）
    with pytest.raises(McpToolError):
        read("SELECT * FROM t FOR UPDATE")


@pytest.mark.parametrize("sql", [
    "prepare s as select 1",          # 小写首关键词同样拦
    "execute s",
    "PREPARE s AS DELETE FROM t",     # polardb 绕过面的变体
])
def test_adversarial_prepare_execute_variants(sql):
    with pytest.raises(McpToolError):
        read(sql)


def test_adversarial_with_utility_keyword_rejected():
    # WITH 语句内出现 UTILITY 关键字 → 拒绝（纯词法扫描，不要求语法合法）
    with pytest.raises(McpToolError):
        read("WITH c AS (SELECT 1) LOCK TABLE t")


def test_adversarial_with_hides_dangerous_function():
    # WITH 定型前先做危险函数检查
    with pytest.raises(McpToolError):
        read("WITH c AS (SELECT pg_sleep(1)) SELECT * FROM c")


@pytest.mark.parametrize("sql,risk,kind", [
    ("TABLE t", ToolRisk.READ, "TABLE"),                                  # READ 白名单第 5 词
    ("DELETE FROM t WHERE id = 1", ToolRisk.WRITE, "DELETE"),
    (
        "MERGE INTO t USING s ON (t.id = s.id) WHEN MATCHED THEN UPDATE SET t.a = s.a",
        ToolRisk.WRITE,
        "MERGE",
    ),
    ("REPLACE INTO t VALUES (1)", ToolRisk.WRITE, "REPLACE"),
    ("ALTER TABLE t ADD COLUMN c int", ToolRisk.DANGEROUS, "ALTER"),
    ("RENAME TABLE a TO b", ToolRisk.DANGEROUS, "RENAME"),
    ("COMMENT ON TABLE t IS 'x'", ToolRisk.DANGEROUS, "COMMENT"),
    ("GRANT SELECT ON t TO u", ToolRisk.DANGEROUS, "GRANT"),
    ("REVOKE SELECT ON t FROM u", ToolRisk.DANGEROUS, "REVOKE"),
])
def test_adversarial_whitelist_matrix(sql, risk, kind):
    # 白名单全集逐词覆盖：每个首关键词都走对通道（含 GRANT/REVOKE 子句里的 SELECT 不影响 DDL 通道）
    result = read(sql)
    assert (result.risk, result.kind) == (risk, kind)


# ---------- A/ORA 兼容：层次查询 START WITH 子句（词法修复回归） ----------
def test_acompat_hierarchical_query_read():
    # A 兼容层次查询：START WITH 是 SELECT 的合法子句（与 CONNECT BY 配套），
    # 不得因 START 命中 UTILITY 扫描而误拒；START 语句形态由首关键词门拦截
    assert (
        read("SELECT a FROM t START WITH a = 1 CONNECT BY PRIOR a = a").risk
        is ToolRisk.READ
    )


def test_start_transaction_still_rejected_not_whitelisted():
    # START 作语句首关键词：白名单没有它 → NOT_WHITELISTED（防线只剩首关键词门）
    with pytest.raises(McpToolError) as exc:
        read("START TRANSACTION")
    assert exc.value.code == "GAUSSDB_DATA_MCP_NOT_WHITELISTED"


def test_start_lowercase_first_keyword_rejected():
    with pytest.raises(McpToolError) as exc:
        read("start transaction")
    assert exc.value.code == "GAUSSDB_DATA_MCP_NOT_WHITELISTED"


# ---------- 规格审查修复：双引号标识符穿透（String.Symbol 剥引号归一） ----------
def test_quoted_identifier_function_call_rejected():
    # sqlparse 把 PG 双引号标识符标为 String.Symbol 而非 Name；
    # 剥掉两侧双引号后与危险函数名单匹配，'(' 锚定不变
    with pytest.raises(McpToolError):
        read('SELECT "pg_sleep"(100)')


def test_quoted_identifier_set_config_rejected():
    # 同路径同时穿透函数层与变量层（变量名在单引号字符串里是数据，由函数层拦截）
    with pytest.raises(McpToolError):
        read('SELECT "set_config"(\'session_replication_role\',\'replica\',false)')


def test_quoted_identifier_sensitive_var_rejected():
    with pytest.raises(McpToolError):
        read('ALTER ROLE r SET "row_security" = off')


def test_single_quoted_string_is_data_not_flagged():
    # 单引号字符串（String.Single）是数据字面量，不参与标识符匹配——不误报
    assert read("SELECT 'pg_sleep'").risk is ToolRisk.READ


def test_quoted_column_without_call_not_flagged():
    # 带引号列名无括号调用——函数层不误报（与不带引号的列名行为一致）
    assert read('SELECT "pg_sleep" FROM t').risk is ToolRisk.READ


# ---------- 终审修复 1：U& Unicode 转义与反引号标识符穿透 ----------
def test_unicode_escape_function_call_rejected():
    # PG 9.1+ 语义：U&"\0070g_sleep" 是 Unicode 转义标识符 pg_sleep（\0070 = 'p'）；
    # sqlparse 0.6 切成 Name 'U' + Operator '&' + String.Symbol，三者都不命中名单
    with pytest.raises(McpToolError) as exc:
        read(r'SELECT U&"\0070g_sleep"(1)')
    assert exc.value.code == "GAUSSDB_DATA_MCP_FORBIDDEN_KEYWORD"


def test_unicode_escape_sensitive_var_rejected():
    # 同形态穿透敏感变量层：ALTER ... SET U&"row_security"=off
    with pytest.raises(McpToolError):
        read(r'ALTER ROLE r SET U&"\0072ow_security"=off')


def test_unicode_escape_lowercase_rejected():
    # 小写 u& 是等价的 PG 词法形态（大小写不敏感）
    with pytest.raises(McpToolError):
        read(r'SELECT u&"\0070g_sleep"(1)')


def test_unicode_escape_string_literal_rejected():
    # U&'...' Unicode 转义字符串形态同样拒绝（归一化无法穷举，保守全拒）
    with pytest.raises(McpToolError):
        read(r"SELECT U&'d\0061tA'")


def test_backtick_function_call_rejected():
    # sqlparse 0.6 把 `pg_sleep` 整体标为 Name 且值保留反引号，upper 后
    # 不匹配名单——GaussDB B 兼容库下反引号是合法标识符定界符，会真执行
    with pytest.raises(McpToolError) as exc:
        read("SELECT `pg_sleep`(1)")
    assert exc.value.code == "GAUSSDB_DATA_MCP_FORBIDDEN_FUNCTION"


def test_backtick_sensitive_var_rejected():
    with pytest.raises(McpToolError):
        read("ALTER ROLE r SET `row_security` = off")


def test_backtick_column_without_call_not_flagged():
    # 反引号列名无括号调用——不误报（与双引号/裸列名行为一致）
    assert read("SELECT `pg_sleep` FROM t").risk is ToolRisk.READ


# ---------- Oracle 语法深化轮（2026-10-12）：集合运算符 / 序列 / Oracle 读形态 ----------
# 矩阵实测依据：本地集中式 A 库真库（词法 + 只读会话行为），
# live 锁定见 tests/integration/test_live_acompat.py（TestOracle* 系列）。
@pytest.mark.parametrize("sql", [
    "SELECT * FROM t1 MINUS SELECT * FROM t2",          # Oracle 集合运算符（PG 叫 EXCEPT）
    "SELECT * FROM t1 EXCEPT SELECT * FROM t2",
    "SELECT * FROM t1 INTERSECT SELECT * FROM t2",
    "SELECT DECODE(a, 1, 'x', 'y') FROM t",
    "SELECT TO_CHAR(sysdate, 'YYYY-MM-DD') FROM DUAL",
    "SELECT /*+ FULL(t) */ * FROM t",                    # Oracle 优化器提示在注释里
])
def test_oracle_read_forms_ok(sql):
    assert read(sql).risk is ToolRisk.READ


@pytest.mark.parametrize("sql", [
    "SELECT seq_test.NEXTVAL FROM DUAL",   # Oracle 属性形态（内核改写为 nextval()）
    "SELECT nextval('s')",
    "SELECT setval('s', 1)",
])
def test_sequence_advancement_lexically_read(sql):
    # 序列推进词法层放行为 READ（真状态变更）。真库实测（507 集中式 A 库）：第二层
    # 只读会话由内核兜底——ReadOnlySqlTransaction "cannot execute nextval()/setval()
    # in a read-only transaction"（live 用例锁定），故词法层无需再堵。
    assert read(sql).risk is ToolRisk.READ


def test_sequence_currval_lexically_read():
    # CURRVAL 只读：词法放行；会话内未成功 NEXTVAL 时数据库报 currval undefined（live 锁定）
    assert read("SELECT seq_test.CURRVAL FROM DUAL").risk is ToolRisk.READ


@pytest.mark.parametrize("sql", [
    "INSERT INTO t (id) VALUES (seq_test.NEXTVAL)",   # Oracle 主线 DML 模式
    "INSERT INTO t VALUES (nextval('s'))",
])
def test_sequence_in_dml_stays_write(sql):
    # nextval/setval 刻意不进 _DANGEROUS_FUNCTIONS 的原因：写通道的
    # INSERT ... VALUES (seq.NEXTVAL) 是合法写语句（READ_ONLY=false 环境的正道用法），
    # 序列只读兜底交给第二层（只读会话），词法层不得误伤写通道。
    assert read(sql).risk is ToolRisk.WRITE


@pytest.mark.parametrize("sql,kind", [
    ("CREATE SEQUENCE seq_test START WITH 1", "CREATE"),   # START WITH 子句不误拒
    ("DROP SEQUENCE seq_test", "DROP"),
    ("ALTER SEQUENCE seq_test RESTART WITH 5", "ALTER"),
])
def test_sequence_ddl_dangerous(sql, kind):
    # 序列 DDL 与表 DDL 一致：走 DANGEROUS 通道（confirm 门 + ENABLE_DDL 开关）
    result = read(sql)
    assert (result.risk, result.kind) == (ToolRisk.DANGEROUS, kind)


# ---------- 终审修复 2：咨询锁函数进危险名单 ----------
@pytest.mark.parametrize("sql", [
    "SELECT pg_advisory_lock(1)",              # 会话级：持有到会话结束（池化危害链）
    "SELECT pg_try_advisory_lock(1)",
    "SELECT pg_advisory_xact_lock(1)",
    "SELECT pg_try_advisory_xact_lock(1)",
    "INSERT INTO t VALUES (pg_advisory_lock(1))",  # 写通道同样拦
])
def test_advisory_lock_functions_rejected(sql):
    # 错误文案早已承诺 "advisory locks"，此处兑现名单
    with pytest.raises(McpToolError) as exc:
        read(sql)
    assert exc.value.code == "GAUSSDB_DATA_MCP_FORBIDDEN_FUNCTION"
