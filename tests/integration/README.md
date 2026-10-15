# Live 集成测试（真实华为云 GaussDB 实例）

在真实 GaussDB 507 实例上端到端验证全部 7 个工具（三通道 execute / explain /
list_schemas / list_tables / describe_table），覆盖只读拦截、确认门、DDL 开关与审计
落盘。第一次真库全链路验证（Task 9）。

## 环境要求

| 项 | 说明 |
| --- | --- |
| Docker | 本机 Docker Desktop 运行中（测试在 `python:3.11-slim` 容器内执行） |
| 测试实例 | 华为云 BJ4 集中式 `mcp-sql-test-bj4`（内核 507.0.0），公网 EIP `1.94.240.147:8000`，安全组已放行本机出口 IP |
| 实例凭证 | `ops/create-bj4.json`（创建实例时的 payload，`password` 字段即管理员 `root` 的密码；该文件不入库） |
| 自研驱动 whl | `ops/driver-unpack/python-driver/python_driver/Euler2.10/GaussDB_Kernel_507_0_0-2.9.10-py311-none-linux_x86_64.whl`（仅 Linux/py311，故宿主 Windows 不能直跑） |
| 网络 | 容器内 pip/apt 走清华源；实例走公网 EIP + `sslmode=require`（公网地址触发项目的强 SSL 护栏，这是预期行为） |

## 运行方式

仓库根执行：

```bash
bash gaussdb-data-mcp/tests/integration/run_live.sh
```

runner 是自包含的：宿主机段用 Docker 投递容器（仓库挂载为只读 `/repo`），容器内段
apt/pip 装依赖 → 组装凭证 → 跑 `pytest tests/integration -m live -v` → 附加
streamable-http 冒烟（无 token 401 / 带 Bearer token initialize 200）。预期：

- 10 个 live 测试全 PASSED；
- 末行 `LIVE SUITE OK`。

本机日常 `uv run pytest` 不受影响：live 测试带 `marker=live` 且未设
`GAUSSDB_DATA_MCP_CREDENTIALS_FILE` 时整文件 skip。

## A 兼容专项（run_live_acompat.sh）

```bash
bash gaussdb-data-mcp/tests/integration/run_live_acompat.sh
```

默认指向 GY1 分布式实例（`LIVE_HOST` / `LIVE_PASSWORD_JSON` 可覆盖）。流程：

1. `acompat_provision.py create --compat ORA,A,PG,B,C`：经 MCP
   `execute_sql_dangerous`（confirm 流程）依次尝试建
   `CREATE DATABASE _acompat_probe DBCOMPATIBILITY = '<X>'`，每步信封原样打印；
   第一个成功的值胜出并写出 A 库凭证（`/tmp/live-credentials-acompat.json`）。
   全部被拒时 runner 以探测结论退出（报错原文已在输出里）。
2. `pytest tests/integration/test_live_acompat.py -m live`：A 语义专项
   （DUAL / 空串即 NULL / 层次查询 / MERGE 往返 / ON CONFLICT → EXEC_FAILED /
   序列只读兜底 / MINUS·INTERSECT / DECODE·TO_CHAR·优化器提示注释）。
   目标库非 A/ORA 兼容时整文件 skip（真实模式留在 skip reason）。
3. `acompat_provision.py drop` 清理探测库。

本地集中式轨道（n8n_test，A 库，开源 psycopg2-binary，不经容器）：

```bash
GAUSSDB_DATA_MCP_ACOMPAT_CREDENTIALS_FILE=../ops/local-acompat-credentials.json \
  uv run --group local pytest tests/integration/test_live_acompat.py -m live -v
```

（凭证文件在 `ops/` 下不入库；`--group local` 见 pyproject 与主 README §2.4。）

## 数据保真度专项（test_live_data_fidelity.py，教训 L8 固化）

此前 live 数据全是 ASCII——issue #1 用户发现 pyodbc 中文乱码才暴露这个盲区。本文件
把中国客户场景的真实数据形态锁进真库回归：中文（常用/生僻混繁体/首尾空格）、特殊
字符（单引号/反斜杠/真实换行/emoji）、NULL vs 空串（A 兼容语义）、5000 字长文本
（全量读回 + max_cell_chars 截断标记）、数字边界（int64 上限/0.1/3.14159）。

两轨各跑一遍（凭证门控 `GAUSSDB_DATA_MCP_FIDELITY_CREDENTIALS_FILE`，未设整文件
skip）：

```bash
cd gaussdb-data-mcp
# psycopg2 轨（本地容器）
GAUSSDB_DATA_MCP_FIDELITY_CREDENTIALS_FILE=../ops/local-acompat-credentials.json \
GAUSSDB_DATA_MCP_DRIVER=psycopg2 \
  uv run --group local pytest tests/integration/test_live_data_fidelity.py -m live -v
# pyodbc 轨（Windows 原生）——必须用 python.org 官方构建的解释器（uv 托管构建的
# libcrypto-3-x64.dll 与 gsqlodbc 同名冲突，IM003 (160)，见主 README §2.5）：
py -3.13 -m venv .venv-odbc && uv pip install --python .venv-odbc/Scripts/python.exe \
  "mcp>=2,<3" "sqlparse>=0.6,<0.7" pyodbc pytest pytest-asyncio wheels/huaweicloud_mcp_core-1.1.1-py3-none-any.whl -e .
GAUSSDB_DATA_MCP_FIDELITY_CREDENTIALS_FILE=../ops/local-acompat-credentials.json \
GAUSSDB_DATA_MCP_DRIVER=pyodbc .venv-odbc/Scripts/python.exe -m pytest \
  tests/integration/test_live_data_fidelity.py -m live -v
```

**实测结果（本地集中式 A 库 n8n_test，127.0.0.1:5432，507.0.0，2026-10-15）**：
psycopg2 轨 6/6、pyodbc 轨 6/6。首轮 pyodbc 轨抓到两个真问题并当场处置：

1. **换行失真（已修）**：gsqlodbc 读路径默认把 LF 规范化成 CRLF（服务端
   `octet_length` 实测写入无损、仅读取被改写）——服务连接串加 `LFConversion=0`
   修复，读回与存储逐字节一致（drivers.py，与 issue #1 的 ansi=False 同类修复）。
2. **布尔字符串化跨轨不一致（测试侧规避）**：`'' IS NULL` 原生布尔经 psycopg2
   得 'True'、经 pyodbc 得 '1'——保真断言改用 `CASE WHEN ... THEN 'yes'` 显式
   字符串化，不依赖驱动布尔表示。

预探测事实（同日）：`standard_conforming_strings=on`、`server_encoding=UTF8`、
字面量用 `E''` 形态显式化（不依赖 s_c_s）、A 库空串入库即 NULL、appuser 有 DDL 权限。

副作用：探针表 `_mcp_fidelity_probe` 建删走 dangerous 通道确认流程，finally 兜底 DROP。

## 凭证来源

容器内由 runner 从 `/repo/ops/create-bj4.json` 读取 `password`，生成 0600 的
`/tmp/live-credentials.json`：

```json
{
  "host": "1.94.240.147",
  "port": 8000,
  "dbname": "postgres",
  "user": "root",
  "password": "<来自 ops/create-bj4.json>",
  "sslmode": "require"
}
```

密码只在容器文件系统内流转，不进命令行参数、不回显。换实例时改 runner 中
`creds` 一段与 `ops/create-bj4.json`。

## 测试副作用声明

`TestWriteChannels` 会在目标库 `public` schema **建立并删除表 `_mcp_live_probe`**
（`CREATE TABLE` → `INSERT` → `SELECT` → `DROP TABLE`，失败路径 finally 里
`DROP TABLE IF EXISTS` 兜底清理）。这是专用测试实例，可接受；不要对生产实例跑
live 套件。

## 实测备案（survey/03 补充）

- `information_schema.schemata` 返回系统 schema（`dbe_*` / `pkg_*` / `rds*` /
  `sqladvisor` 等）+ `public`；初始账号是 `root`，`$user` 解析为 `root` schema
  （`search_path="$user",public`），**不存在名为 `postgres` 的 schema**。
- `EXPLAIN` 的结果列名是 `====== Query Others =====`（非 PG 的 `QUERY PLAN`），
  常量查询的计划行形如 `Parameterize: Yes, parameterized sql: SELECT ?`。

## A 兼容实测备案（2026-10，集中式本地容器 + 分布式 GY1）

同为 507 内核，两形态的兼容模式参数**互不通用**（镜像差异）：

| 项 | 集中式（本地容器 507.0.0） | 分布式（GY1 507.0.0） |
| --- | --- | --- |
| `CREATE DATABASE x DBCOMPATIBILITY = 'A'` | 成功 | 拒：`InvalidParameterValue: Compatibility args A is invalid` |
| `CREATE DATABASE x DBCOMPATIBILITY = 'ORA'` | 拒：`InvalidParameterValue: Compatibility args ORA is invalid` | 成功 |
| `pg_database.datcompatibility` 取值 | `A` / `M` | `ORA` / `MYSQL` / `M`（全称） |
| 默认 postgres 库兼容 | A | MYSQL |
| SQL 建库（root） | 可 | 可（无需管理面） |

A/ORA 库上的语法行为（两形态一致，5/5 专项用例通过）：DUAL 伪表、空串即 NULL
（`SELECT ''` 返回 NULL）、层次查询 `START WITH ... CONNECT BY`、`MERGE INTO`
往返均正常；PG 式 `ON CONFLICT` 被数据库拒绝（`syntax error at or near "CONFLICT ("`）
→ 服务端映射为 `GAUSSDB_DATA_MCP_EXEC_FAILED` 信封。另：管控面 OpenAPI
`create_database` 请求体无 DBCOMPATIBILITY 字段（仅 name/character_set/owner/
template/lc_collate/lc_ctype），建 A 库不能靠它（除非 template 指向 A 模板库）。

## Oracle 语法深化实测备案（2026-10-12，本地集中式 A 库 + 云端 BJ4 抽验 10/10）

- **序列三层兜底**：`seq.NEXTVAL`（内核改写为 nextval()）/ `nextval()` / `setval()`
  词法层归 READ（不入危险函数名单）；第二层只读会话由内核拒绝——
  `ReadOnlySqlTransaction: cannot execute nextval()/setval() in a read-only
  transaction` → `GAUSSDB_DATA_MCP_EXEC_FAILED`。`CURRVAL` 只读放行，但读池连接上
  不可能有成功的 NEXTVAL，总是报 `Currval of sequence ... is not yet defined in
  this session`。写通道 `INSERT ... VALUES (seq.NEXTVAL)` 保持 WRITE 可用（Oracle
  主线 DML，词法层不得误伤——不进危险函数名单的原因）；`CREATE/DROP SEQUENCE`
  走 DDL 确认门。
- **集合运算符**：`MINUS`（Oracle 形态）与 `EXCEPT`（PG 形态）在集中式 A 库均可用
  且语义等价，`INTERSECT` 正常；三者词法层均归 READ。
- **Oracle 读形态**：`DECODE`、`TO_CHAR(sysdate, 'YYYY-MM-DD')`、优化器提示注释
  `/*+ FULL(t) */`（提示在注释里，不触发词法关键字扫描）真库验证通过。
- BJ4 抽验：`run_live_acompat.sh` + `LIVE_HOST=1.94.240.147
  LIVE_PASSWORD_JSON=/repo/ops/create-bj4.json`，建库探测行为与上表一致
  （ORA 被拒 → 'A' 成功），10/10 通过，探针库已清理。

## Dify 手工冒烟清单（供人工执行）

1. 起服务（宿主机或部署机；`MCP_SERVER_HOST=0.0.0.0` 时必须提供 tokens 文件，
   否则拒绝启动，fail-closed）：

   ```bash
   GAUSSDB_DATA_MCP_CREDENTIALS_FILE=/abs/live-credentials.json \
   GAUSSDB_DATA_MCP_TOKENS_FILE=/abs/tokens.json \
   MCP_SERVER_HOST=0.0.0.0 \
     gaussdb-data-mcp -t streamable-http
   ```

   `tokens.json`（0600）：`{"tokens": {"dify": "tok_xxx"}}`

2. 自检：`curl` 无 `Authorization` 头 → 401；带 `Authorization: Bearer tok_xxx` 的
   initialize 请求 → 200（run_live.sh 冒烟段即此逻辑）。
3. Dify → 工具 → MCP → 添加 `http://<host>:8000/mcp`，鉴权选 Custom Header：
   `Authorization: Bearer tok_xxx`。
4. 在 Dify 应用里让 agent 依次调 `list_schemas` → `list_tables` →
   `execute_sql("SELECT 1")`，完成一次真实查询；确认 agent 能看到结果行。
5. （可选）验证只读默认：让 agent 尝试 `execute_sql_write` → 应返回
   `MCP_READ_ONLY` 错误信封。
