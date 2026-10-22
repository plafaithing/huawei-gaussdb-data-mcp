# GaussDB Data MCP Server

> **语言切换：** [English](./README.md) | 中文

## 版本

0.1.0

## 概述

华为云 GaussDB 数据面 MCP Server：AI 客户端经标准 MCP 协议在三层安全防护下查询与
操作 GaussDB 数据，支持双传输与结构化审计。本服务连接的是数据库实例本身（数据库
协议 + 数据库账号），凭证只保存在服务端、不经过 LLM；与管控面 GaussDB MCP
（OpenAPI/AK-SK 管实例生命周期）互补——那边管云资源，这边管库内数据。

## 分类

数据库

## 功能

- 7 个工具：SQL 执行（读 / 写 / DDL）、EXPLAIN、schema/表/字段浏览
- 三档风险通道（READ / WRITE / DANGEROUS），DANGEROUS 通道走确认信封，且默认关闭、
  需 `GAUSSDB_DATA_MCP_ENABLE_DDL=true` 显式开启
- 服务端 SQL 白名单分类 + 通道锁：语句由服务端分类（不依赖 LLM 选对通道），必须走
  与风险匹配的工具（否则 `GAUSSDB_DATA_MCP_RISK_MISMATCH`）；多语句、UTILITY 语句、
  危险函数、敏感会话变量一律默认拒绝
- 代码级只读默认（`GAUSSDB_DATA_MCP_READ_ONLY=true`）；读连接建立即固定只读事务，
  归还池前 `RESET ALL`
- 读连接池（指纹校验、空闲 TTL）；写/DDL 连接用完即关
- 双驱动：GaussDB 官方 psycopg2 驱动（Linux/py3.11，连云上实例必须）+ pyodbc
  （Windows，经 GaussDB ODBC 驱动）
- streamable-http 内置 Bearer token 鉴权（常数时间比较、401/503 语义、token 文件热读）
- 每次工具调用一行 JSONL 审计（工具、风险、决策、规则命中、客户端身份、脱敏入参）
- 热轮换：credentials.json 与 tokens.json 每次调用重读，原子替换即生效，无需重启
- Oracle 兼容（A/ORA）库为第一优先场景：MERGE INTO upsert、DUAL、层次查询、MINUS、
  序列、DECODE 等已在真实集中式与分布式实例上验证

## 工具清单

数据面不暴露云 API，所有工具执行的都是 SQL，故每行链接覆盖该工具语句族的
GaussDB SQL 语法参考。

| 工具 | 场景 | 风险级别 | 文档 |
|---|---|---|---|
| `execute_sql` | 单条只读语句：SELECT / VALUES / SHOW / EXPLAIN / TABLE / WITH…SELECT——数据查询与探索 | READ | [GaussDB SQL 语法参考](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |
| `execute_sql_write` | 单条 DML：INSERT / UPDATE / DELETE / MERGE / REPLACE（autocommit；需 `READ_ONLY=false`） | WRITE | [GaussDB SQL 语法参考](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |
| `execute_sql_dangerous` | 单条 DDL：CREATE / ALTER / DROP / TRUNCATE / RENAME / COMMENT / GRANT / REVOKE（确认信封 + `ENABLE_DDL=true`） | DANGEROUS | [GaussDB SQL 语法参考](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |
| `explain_sql` | 对 SELECT 类语句跑 EXPLAIN 不执行——大查询前先看计划 | READ | [GaussDB SQL 语法参考](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |
| `list_schemas` | 列出 MCP 账号可见的 schema | READ | [GaussDB SQL 语法参考](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |
| `list_tables` | 列出 schema 内表与视图（参数化查询） | READ | [GaussDB SQL 语法参考](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |
| `describe_table` | 表字段详情：列名/类型/可空/默认值（参数化查询） | READ | [GaussDB SQL 语法参考](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |

所有工具每次调用恰好执行一条语句。

## 使用指引

### 前置条件

- Python 3.10+ 与 [uv](https://docs.astral.sh/uv/)：

**Linux/macOS：**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows：**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

- **数据库驱动。** 云上 GaussDB 实例默认 sha256 认证，开源 psycopg2 不支持——须用
  官方自研驱动：

  - **Linux（生产）：** 官方 psycopg2 驱动不发布 PyPI，只随数据库驱动发布包分发
    （仅 Linux x86_64/ARM64 + Python 3.11）。用 `./fetch-driver.sh
    /path/to/DBS-GaussDB-driver_*.tar.gz` 提取 whl，然后 `pip uninstall psycopg2
    psycopg2-binary` 再安装提取出的 whl。系统依赖：`libkeyutils1 libsasl2-2
    libldap-common`；非数据库用户运行时需设 `LD_LIBRARY_PATH` 指向
    `<site-packages>/gaussdb_psycopg2.lib`。
  - **Windows（原生）：** 安装 GaussDB ODBC 驱动（官方驱动包内 `gsqlodbc` 安装器，
    注册名为 `GaussDB Unicode`）与 `pyodbc`（`uv sync --group windows`）。必须使用
    python.org 官方安装包的 Python（3.11+）：uv 托管等第三方构建的 Python 其
    `_ssl.pyd` 依赖 `libcrypto-3-x64.dll`，与 GaussDB ODBC 驱动的同名 DLL 冲突
    （症状 `IM003 (160)`）；官方构建命名为 `libcrypto-3.dll`，无冲突。

- **数据库账号（第三层防护）。** 绝不使用初始管理员账号——创建专用最小权限角色。
  只读基线（默认部署形态）：

  ```sql
  CREATE ROLE mcp_ro WITH LOGIN PASSWORD '********';
  GRANT USAGE ON SCHEMA mybiz TO mcp_ro;
  GRANT SELECT ON ALL TABLES IN SCHEMA mybiz TO mcp_ro;        -- 现有表
  ALTER DEFAULT PRIVILEGES IN SCHEMA mybiz
    GRANT SELECT ON TABLES TO mcp_ro;                          -- 未来新建表
  ```

  放开写入时用单独角色，只授业务 schema 的 SELECT/INSERT/UPDATE/DELETE——不授
  CREATE/ALTER/DROP/TRUNCATE/GRANT，即使 DDL 开关误开，账号层仍拦得住。GaussDB
  密码连错 10 次锁号 1 天；本服务对认证失败刻意不重试。

### 安装

```bash
git clone https://github.com/huaweicloud/huaweicloud-mcp.git
cd huaweicloud-mcp/server/mcp_server_gaussdb_data
uv sync
uv run mcp_server_gaussdb_data                     # stdio（默认）
uv run mcp_server_gaussdb_data -t streamable-http  # 远程传输
```

Linux 上先按「前置条件」装好官方驱动 whl（`./fetch-driver.sh` 可从官方驱动包提取）。

## 配置

### 环境变量

数据库凭证可来自 credentials.json 文件（`GAUSSDB_DATA_MCP_CREDENTIALS_FILE`，0600、
热重读）或直接来自 `GAUSSDB_DATA_MCP_DB_*` 环境变量；两者同设时文件优先。

| 变量 | 必填 | 说明 | 默认值 |
|---|---|---|---|
| `GAUSSDB_DATA_MCP_READ_ONLY` | 否 | `true` 拦截一切 WRITE/DANGEROUS 工具（中间件启动快照——改动需重启） | `true` |
| `GAUSSDB_DATA_MCP_ENABLE_DDL` | 否 | 打开 DANGEROUS（DDL）通道；还需 `READ_ONLY=false`。热读 | `false` |
| `GAUSSDB_DATA_MCP_CREDENTIALS_FILE` | 否 | credentials.json 路径（0600；每次调用热重读） | 空 |
| `GAUSSDB_DATA_MCP_TOKENS_FILE` | 否 | HTTP Bearer token 文件；非回环监听时必配 | 空 |
| `GAUSSDB_DATA_MCP_GUARDRAILS_CONFIG` | 否 | guardrails 规则 YAML（block/confirm/warn） | 空 |
| `GAUSSDB_DATA_MCP_AUDIT_FILE` | 否 | 审计 JSONL 文件（审计始终同时写 stderr） | 空 |
| `GAUSSDB_DATA_MCP_PROTECTED_IDS` | 否 | 护栏受保护资源 ID（CSV） | 空 |
| `GAUSSDB_DATA_MCP_PROTECTED_NAMES` | 否 | 护栏受保护资源名（CSV） | 空 |
| `GAUSSDB_DATA_MCP_PROTECTED_TAGS` | 否 | 护栏受保护资源标签（CSV） | 空 |
| `MCP_SERVER_HOST` | 否 | streamable-http 监听地址；非回环须配 `TOKENS_FILE` | `127.0.0.1` |
| `MCP_SERVER_PORT` | 否 | streamable-http 端口（1..65535） | `8000` |
| `GAUSSDB_DATA_MCP_MAX_ROWS` | 否 | 单次查询返回行数上限，超出截断并标记 | `200` |
| `GAUSSDB_DATA_MCP_MAX_CELL_CHARS` | 否 | 单元格字符截断上限 | `256` |
| `GAUSSDB_DATA_MCP_MAX_SQL_CHARS` | 否 | 单条 SQL 长度上限 | `65536` |
| `GAUSSDB_DATA_MCP_STATEMENT_TIMEOUT_MS` | 否 | 服务端 statement_timeout（GaussDB 默认 0 = 不超时） | `30000` |
| `GAUSSDB_DATA_MCP_POOL_MIN` | 否 | 读连接池最小连接数 | `1` |
| `GAUSSDB_DATA_MCP_POOL_MAX` | 否 | 读连接池最大连接数 | `8` |
| `GAUSSDB_DATA_MCP_APPLICATION_NAME` | 否 | 连接 application_name（便于在 `pg_stat_activity` 识别 MCP 流量） | `gaussdb_data_mcp` |
| `GAUSSDB_DATA_MCP_DRIVER` | 否 | 强制驱动后端：`psycopg2` / `pyodbc`（自动：Windows → pyodbc，其余 → psycopg2） | 自动 |
| `GAUSSDB_DATA_MCP_ODBC_DRIVER` | 否 | pyodbc 轨的 ODBC 驱动注册名 | `GaussDB Unicode` |
| `GAUSSDB_DATA_MCP_DB_HOST` | 否 | 无凭证文件时的环境变量兜底；host/port 刻意无默认值——GaussDB CN 端口通常为 **8000**，驱动默认 5432 是错的 | 空 |
| `GAUSSDB_DATA_MCP_DB_PORT` | 否 | 数据库端口 | `0`（必须显式给值） |
| `GAUSSDB_DATA_MCP_DB_DBNAME` | 否 | 数据库名 | 空 |
| `GAUSSDB_DATA_MCP_DB_USER` | 否 | 数据库用户 | 空 |
| `GAUSSDB_DATA_MCP_DB_PASSWORD` | 否 | 数据库密码 | 空 |
| `GAUSSDB_DATA_MCP_DB_SSLMODE` | 否 | `disable` / `allow` / `prefer` / `require` / `verify-ca` / `verify-full`；公网地址低于 `require` 拒绝服务（fail-closed） | `prefer` |
| `GAUSSDB_DATA_MCP_DB_SSLROOTCERT` | 否 | CA 证书路径（verify-ca/verify-full；pyodbc 轨必须显式配置） | 空 |
| `GAUSSDB_DATA_MCP_DB_SSLCERT` | 否 | 客户端证书（gs_hba cert 认证） | 空 |
| `GAUSSDB_DATA_MCP_DB_SSLKEY` | 否 | 客户端私钥（gs_hba cert 认证） | 空 |

credentials.json 字段：`host`、`port`、`dbname`、`user`、`password`、`sslmode`
（可选 `sslrootcert` / `sslcert` / `sslkey`）。示例见 `credentials.example.json` 与
`tokens.example.json`。

### 保护名单盲区

护栏规则匹配的是**工具参数**：工具入参里的资源 ID、名称类参数与显式标签。本服务
数据面工具的入参是 SQL 文本（`query`、`schema`、`table`），因此参数级资源匹配
（`resources.ids` / `id_prefixes` / `name_patterns` / `tags`）对 SQL 文本不生效——
无法按对象名保护语句内的数据库对象。本服务的保护分两级：

1. **工具级（护栏，生效）：** 按 `tools` / `risks` 匹配规则——例如整体 block/confirm
   DANGEROUS 通道：

   ```yaml
   rules:
     - id: protect-ddl-channel
       action: block          # block / confirm / warn
       message: "DDL is blocked on this deployment; use the approved change window."
       match:
         tools: ["execute_sql_dangerous"]
         risks: ["DANGEROUS"]
   ```

   （另见 `guardrails.example.yaml`。）

2. **SQL 级（三层数据防护）：** 语句经 SQL 白名单分类并与工具通道锁强制一致
   （第一层）、读池连接只读会话（第二层）、数据库最小权限账号（第三层）。
   `GAUSSDB_DATA_MCP_PROTECTED_*` 变量走同一条护栏管线，在本服务中同样只在工具级
   生效；SQL 级保护由上述三层承担。

规则在 core 中间件层评估，与只读门、confirm 门叠加；评估器异常时 fail-closed
（写通道阻断、读通道放行）。

### uvx 运行

git 仓库版（subdirectory 安装）：

```json
{
  "mcpServers": {
    "gaussdb-data": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/huaweicloud/huaweicloud-mcp#subdirectory=server/mcp_server_gaussdb_data",
        "mcp_server_gaussdb_data"
      ],
      "env": {
        "GAUSSDB_DATA_MCP_CREDENTIALS_FILE": ""
      }
    }
  }
}
```

PyPI 版配置（`"command": "uvx", "args": ["mcp_server_gaussdb_data"]`）待包发布到
PyPI 后补充。

## 示例

### Claude Code（stdio）

`.mcp.json`（项目级）或 `~/.claude.json`：

```json
{
  "mcpServers": {
    "gaussdb-data": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/huaweicloud-mcp/server/mcp_server_gaussdb_data",
        "mcp_server_gaussdb_data"
      ],
      "env": {
        "GAUSSDB_DATA_MCP_CREDENTIALS_FILE": "/path/to/credentials.json",
        "GAUSSDB_DATA_MCP_AUDIT_FILE": "/path/to/audit.jsonl"
      }
    }
  }
}
```

### Cursor / 远程客户端（streamable-http + Bearer）

启动服务：

```bash
GAUSSDB_DATA_MCP_CREDENTIALS_FILE=/path/to/credentials.json \
GAUSSDB_DATA_MCP_TOKENS_FILE=/path/to/tokens.json \
MCP_SERVER_HOST=127.0.0.1 \
  uv run mcp_server_gaussdb_data -t streamable-http
```

客户端配置：

```json
{
  "mcpServers": {
    "gaussdb-data": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

自然语言使用：

- "列出我能看到哪些 schema 和表" → `list_schemas` / `list_tables`
- "看下 orders 表有哪些字段" → `describe_table`
- "查本月金额最高的 10 笔订单" → `execute_sql`
- "往 orders 里插一行" → `execute_sql_write`（需 `READ_ONLY=false`）
- "给 orders.created_at 建个索引" → `execute_sql_dangerous`
  （先返回确认信封，复核后带 `confirm=true` 重调）

## 许可证

MIT
