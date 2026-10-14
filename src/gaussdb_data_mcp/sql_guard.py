"""SQL 白名单分类器：把单条 SQL 分类为 READ / WRITE / DANGEROUS，其余一律拒绝。

设计（survey/01 §5、survey/04 §7.1）：
- 首关键词白名单 + sqlparse 全 token 关键字扫描，默认拒绝。
- sqlparse 是词法级解析器而非校验器，本模块的承诺是保守：宁可误拒不漏放。
- 已知保守误报（接受并记录）：
  * 列名/别名恰好是保留字（如 ``SELECT comment FROM t``）会被扫描命中 DDL 关键字；
  * ``SELECT ... FOR UPDATE`` 是合法读锁语句，但 UPDATE 命中 READ 通道扫描；
  * 列名恰好是敏感会话变量名（如 ``SELECT synchronous_commit FROM t``）会被拒绝，
    带双引号的列名（``SELECT "row_security" FROM t``）与不带引号行为一致；
  * ``SELECT u & 'x'``（位与字符串，本身类型不合法）命中 U& 转义三连被拒。

开关与拒绝码（前缀 GAUSSDB_DATA_MCP_）：
EMPTY / NUL_BYTE / TOO_LONG / MULTI_STATEMENT / NOT_WHITELISTED / FORBIDDEN_KEYWORD
/ SENSITIVE_VAR / FORBIDDEN_FUNCTION
"""

from dataclasses import dataclass

import sqlparse
from huaweicloud_mcp_core import ToolRisk
from huaweicloud_mcp_core.errors import McpToolError

DEFAULT_MAX_SQL_CHARS = 65536

_READ_FIRST = frozenset({"SELECT", "VALUES", "SHOW", "EXPLAIN", "TABLE", "WITH"})
_WRITE_FIRST = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE"})
_DDL_FIRST = frozenset(
    {"CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME", "COMMENT", "GRANT", "REVOKE"}
)
# UTILITY：任何通道的 token 扫描命中即拒绝。刻意不含 SET（UPDATE SET / ON CONFLICT 子句）、
# END（CASE END）、ANALYZE（EXPLAIN ANALYZE）、DO（ON CONFLICT DO UPDATE 子句）、
# START（A 兼容层次查询 ``SELECT ... START WITH ... CONNECT BY`` 子句）——
# 这些词作为语句首关键词的形态本身会被下面的首关键词门拦截。
_UTILITY_KEYWORDS = frozenset(
    {
        "PREPARE", "EXECUTE", "COPY", "CALL", "LOCK", "VACUUM", "REINDEX",
        "CLUSTER", "REFRESH", "DISCARD", "LISTEN", "NOTIFY", "LOAD", "IMPORT",
        "EXPORT", "RESET", "BEGIN", "COMMIT", "ROLLBACK", "ABORT",
        "SAVEPOINT", "RELEASE", "CHECKPOINT", "SECURITY",
    }
)
_SCAN_FOR_READ = _WRITE_FIRST | _DDL_FIRST | _UTILITY_KEYWORDS | {"INTO"}
_SCAN_FOR_WRITE = _DDL_FIRST | _UTILITY_KEYWORDS

# survey/05（AWS mysql-mcp 借鉴）：函数级与完整性级补充拒绝
_DANGEROUS_FUNCTIONS = frozenset(
    {
        "PG_SLEEP", "PG_READ_FILE", "PG_READ_BINARY_FILE", "PG_READ_SERVER_FILES",
        "PG_STAT_FILE", "DBLINK", "DBLINK_EXEC", "LO_IMPORT", "LO_EXPORT",
        "SET_CONFIG", "PG_TERMINATE_BACKEND", "PG_CANCEL_BACKEND", "PG_RELOAD_CONF",
        # 咨询锁：会话级形态持有到会话结束——写通道连接用完即关无危害，
        # 但拦截在 READ/WRITE 通道统一执行，不依赖调用方连接生命周期。
        "PG_ADVISORY_LOCK", "PG_TRY_ADVISORY_LOCK",
        "PG_ADVISORY_XACT_LOCK", "PG_TRY_ADVISORY_XACT_LOCK",
    }
)
_SENSITIVE_SESSION_VARS = frozenset(
    {
        "SESSION_REPLICATION_ROLE", "ROW_SECURITY", "SYNCHRONOUS_COMMIT",
        "ZERO_DAMAGED_PAGES", "ACCEPT_FINITE_FLOATS",
    }
)


@dataclass(frozen=True)
class SqlClassification:
    risk: ToolRisk
    kind: str  # 首关键词（大写），供审计与结果展示


def _reject(code: str, message: str, hint: str | None = None) -> None:
    raise McpToolError(f"GAUSSDB_DATA_MCP_{code}", message, hint=hint)


def _is_comment(token: sqlparse.sql.Token) -> bool:
    return token.ttype is not None and token.ttype in sqlparse.tokens.Comment


def _is_semicolon(token: sqlparse.sql.Token) -> bool:
    return (
        token.ttype is not None
        and token.ttype in sqlparse.tokens.Punctuation
        and token.value == ";"
    )


def _statement_tokens(statements: list[sqlparse.sql.Statement]) -> list[sqlparse.sql.Token]:
    """全部语句展平后的 token 流（保持顺序）。"""
    return [token for statement in statements for token in statement.flatten()]


def _first_keyword(statement: sqlparse.sql.Statement) -> str | None:
    """首个非空白、非注释 token 的大写值；非 keyword 时加尖括号（供错误信息展示）。

    走 flatten 而非 token_first：sqlparse 0.6 会把 ``VALUES (1), (2)`` 整体包进
    一个 Values group，token_first 返回的 group 自身不是 keyword。
    """
    for token in statement.flatten():
        if token.is_whitespace or _is_comment(token):
            continue
        value = token.value.upper()
        return value if token.is_keyword else f"<{value}>"
    return None


def _keyword_tokens(tokens: list[sqlparse.sql.Token]) -> set[str]:
    """语句内全部 Keyword 组 token 的大写值（注释/字符串/标识符天然排除）。"""
    return {
        token.value.upper() for token in tokens if token.ttype is not None and token.is_keyword
    }


def _identifier_value(token: sqlparse.sql.Token) -> str | None:
    """标识符 token 的归一值（大写，剥两侧引号/反引号），否则 None。

    覆盖两类标识符形态：
    - ``Token.Name``：普通标识符；sqlparse 0.6 在 MySQL/GaussDB B 兼容形态下
      会把 `` `pg_sleep` `` 整体标为 Name 且**值保留反引号**——不剥则 upper 后
      不匹配名单（终审穿透点）；
    - ``Token.Literal.String.Symbol``：PG 双引号标识符（sqlparse 不标为 Name），
      剥掉两侧双引号后纳入——否则 ``SELECT "pg_sleep"(100)`` 会穿透危险函数层。
    单引号字符串（String.Single）是数据字面量，刻意不在此列。
    """
    if token.ttype is None:
        return None
    if token.ttype in sqlparse.tokens.Name:
        return token.value.strip('`"').upper()
    if token.ttype in sqlparse.tokens.String.Symbol:
        return token.value.strip('"`').upper()
    return None


def _name_tokens(tokens: list[sqlparse.sql.Token]) -> set[str]:
    """语句内全部标识符（Name 及双引号 Symbol，后者剥引号）的大写值。"""
    values = (_identifier_value(token) for token in tokens)
    return {value for value in values if value is not None}


def _function_calls(tokens: list[sqlparse.sql.Token]) -> set[str]:
    """语句内实际被调用（后随 '('，中间允许空白/注释）且在危险名单内的函数名。

    函数名匹配同时覆盖 Name 与双引号 Symbol（剥引号）两种标识符形态。
    """
    calls: set[str] = set()
    for i, token in enumerate(tokens):
        value = _identifier_value(token)
        if value is None or value not in _DANGEROUS_FUNCTIONS:
            continue
        j = i + 1
        while j < len(tokens) and (tokens[j].is_whitespace or _is_comment(tokens[j])):
            j += 1
        if j < len(tokens) and tokens[j].value == "(":
            calls.add(value)
    return calls


def _has_unicode_escape(tokens: list[sqlparse.sql.Token]) -> bool:
    """检测 PG Unicode 转义形态 ``U&"ident"`` / ``u&'string'``（token 间允许空白/注释）。

    sqlparse 0.6 把 ``U&"x"`` 切成 Name 'U' + Operator '&' + String.* 三 token，
    没有任何一个是名单匹配形态；而 PG 9.1+ 语义上它可编码**任意**标识符/字符串
    （``U&"\\0070g_sleep"`` 即 pg_sleep），归一化无法穷举——整体直接拒绝。
    已知保守误报：``SELECT u & 'x'``（位与字符串，本身类型不合法）同样被拒。
    """
    for i, token in enumerate(tokens):
        if (
            token.ttype is None
            or token.ttype not in sqlparse.tokens.Name
            or token.value.upper() != "U"
        ):
            continue
        j = i + 1
        while j < len(tokens) and (tokens[j].is_whitespace or _is_comment(tokens[j])):
            j += 1
        if not (
            j < len(tokens)
            and tokens[j].ttype is not None
            and tokens[j].ttype in sqlparse.tokens.Operator
            and tokens[j].value == "&"
        ):
            continue
        k = j + 1
        while k < len(tokens) and (tokens[k].is_whitespace or _is_comment(tokens[k])):
            k += 1
        if (
            k < len(tokens)
            and tokens[k].ttype is not None
            and tokens[k].ttype in sqlparse.tokens.String
        ):
            return True
    return False


def classify_sql(sql: str, *, max_sql_chars: int = DEFAULT_MAX_SQL_CHARS) -> SqlClassification:
    if not isinstance(sql, str) or not sql.strip():
        _reject("EMPTY", "The SQL statement is empty.")
    if "\x00" in sql:
        _reject("NUL_BYTE", "SQL must not contain NUL bytes (GaussDB driver limitation).")
    if len(sql) > max_sql_chars:
        _reject("TOO_LONG", f"SQL exceeds {max_sql_chars} characters.")

    statements = sqlparse.parse(sql)
    # 语句终结分号之后还允许存在的只有空白；注释或其他任何 token 都视为多语句。
    # 必须在过滤纯注释 statement 之前做：sqlparse 会把 ``SELECT 1; /* c */`` 的尾注释
    # 切成第二个 statement，若先过滤再检查就会漏放。
    tokens = _statement_tokens(statements)
    seen_semicolon = False
    for token in tokens:
        if seen_semicolon and not token.is_whitespace:
            _reject(
                "MULTI_STATEMENT",
                "Content found after the statement-terminating semicolon.",
                hint="Execute statements one at a time.",
            )
        if _is_semicolon(token):
            seen_semicolon = True

    statements = [s for s in statements if s.token_first(skip_ws=True, skip_cm=True)]
    if len(statements) != 1:
        _reject(
            "MULTI_STATEMENT",
            f"Expected exactly one statement, got {len(statements)}.",
            hint="Execute statements one at a time.",
        )
    statement = statements[0]

    first = _first_keyword(statement)
    if first is None:
        _reject("EMPTY", "The SQL statement is empty.")
    keywords = _keyword_tokens(tokens)

    # 终审：U& Unicode 转义可编码任意标识符/字符串，归一化无法穷举——任何通道直接拒绝
    if _has_unicode_escape(tokens):
        _reject(
            "FORBIDDEN_KEYWORD",
            "Unicode-escaped identifiers (U&\"...\") are not supported.",
        )

    # survey/05：敏感会话变量任何通道拒绝（含 DDL——防 ALTER ... SET 关完整性控制）
    var_hit = _name_tokens(tokens) & _SENSITIVE_SESSION_VARS
    if var_hit:
        _reject(
            "SENSITIVE_VAR",
            f"Session variable '{sorted(var_hit)[0]}' controls integrity or security "
            "and cannot be set through this server.",
        )

    if first in _DDL_FIRST:
        return SqlClassification(ToolRisk.DANGEROUS, first)

    # survey/05：危险函数（READ/WRITE 通道；DDL 通道免检——其本体已在确认门内）
    func_hit = _function_calls(tokens)
    if func_hit:
        _reject(
            "FORBIDDEN_FUNCTION",
            f"Function '{sorted(func_hit)[0]}' has server-side side effects "
            "(file access, advisory locks, session control).",
        )

    if first in _WRITE_FIRST:
        hit = keywords & _SCAN_FOR_WRITE
        if hit:
            _reject(
                "FORBIDDEN_KEYWORD",
                f"Keyword '{sorted(hit)[0]}' is not allowed inside a WRITE-channel statement.",
            )
        return SqlClassification(ToolRisk.WRITE, first)

    if first == "WITH":
        if keywords & _UTILITY_KEYWORDS:
            _reject(
                "FORBIDDEN_KEYWORD",
                f"Keyword '{sorted(keywords & _UTILITY_KEYWORDS)[0]}' is not allowed.",
            )
        if keywords & _DDL_FIRST:
            return SqlClassification(ToolRisk.DANGEROUS, "WITH")
        if keywords & _WRITE_FIRST:
            return SqlClassification(ToolRisk.WRITE, "WITH")
        return SqlClassification(ToolRisk.READ, "WITH")

    if first in _READ_FIRST:
        hit = keywords & _SCAN_FOR_READ
        if hit:
            _reject(
                "FORBIDDEN_KEYWORD",
                f"Keyword '{sorted(hit)[0]}' makes this statement non-read-only.",
                hint="If it is a write statement, call execute_sql_write instead.",
            )
        return SqlClassification(ToolRisk.READ, first)

    _reject(
        "NOT_WHITELISTED",
        f"Statement type '{first}' is not whitelisted.",
        hint="Allowed: SELECT / VALUES / SHOW / EXPLAIN / TABLE / WITH (+ write tools).",
    )
