# GaussDB Data MCP Server

GaussDB **数据面 MCP 服务**：AI 客户端（Dify、Claude Code 等）通过它在三层安全防护下查询与操作 GaussDB 业务数据。连接走 **DAS 传输轨**——SQL 经数据管理服务（DAS）的管控面 HTTPS 接口执行，**数据库端口零公网可达**，MCP 可部署在任意能访问华为云管控面的位置（本地 / 云上均可，无需 VPC、专线或 EIP）。

**Oracle 兼容（A/ORA）库是第一优先场景**：MERGE INTO、DUAL、MINUS、DECODE、NVL、varchar2 等 Oracle 系语法已在真实 A 兼容库上实测通过（DAS 轨与 DB 协议轨双验证）。

## 1. 安全模型

| 层 | 机制 |
|---|---|
| 第一层 | 服务端 SQL 白名单分类 + 通道锁（READ/WRITE/DANGEROUS 三工具，语句与通道不符即拒） |
| 第二层 | 最小权限账号（业务操作用 `mcp_ro` 类只读/受限账号，管理员账号仅用于初始化） |
| 第三层 | 凭证域分离：数据库密码只存服务端且**不进入执行链路**（das 轨注册时一次性交给 DAS 托管），凭证/SQL 全程不经 LLM；每次调用一行 JSONL 审计 |

DAS 轨下 MCP 只持 AK/SK（管控面签名）+ 连接 id——数据库密码、连接串均不落在执行路径。

## 2. 安装

```bash
uv sync
uv run gaussdb-data-mcp            # stdio（默认）
uv run gaussdb-data-mcp -t streamable-http
```

## 3. 配置

### 3.1 credentials.json（0600，热轮换）

```json
{
  "ak": "...", "sk": "...", "region": "北京四",
  "default_instance": "<instance_id>",
  "instances": {
    "<instance_id>": {
      "das_connection_id": "<DAS 连接 id>",
      "dbname": "mydb",
      "schema": "dbe_alert",
      "user": "mcp_ro",
      "password": "..."
    }
  }
}
```

| 字段 | 说明 |
|---|---|
| ak/sk | 华为云 AK/SK（管控面签名；DAS 接口与 IAM 自动查 project_id 均使用） |
| region | 支持中文区域名（"北京四"→cn-north-4 自动映射） |
| das_connection_id | DAS 连接 id。来源：DAS 控制台登录实例产生，或 `register_das_connection` 工具注册产生；**缺失时 das 轨自动注册**（需条目内有 user/password） |
| dbname | 执行 SQL 的库。**已配置用户库则直接使用**；落在系统库（postgres 等）时首次调用 `list_user_databases` 获取选择/建库引导 |
| schema | 默认 `dbe_alert`（跨库 DDL 仅在此 schema 下生效，实测约束） |
| user/password | 连接未激活时自动 `retry_login`（带密码——实测唯一可靠激活方式）；建议保留以启用全自动自愈 |

`project_id` 免配置（IAM `/v3/projects` 自动查询并缓存）。多实例：`instances` 下多个条目，`default_instance` 指定默认；所有执行工具有可选 `instance` 参数。

### 3.2 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GAUSSDB_DATA_MCP_DRIVER` | `auto` | 设 `das` 启用 DAS 轨 |
| `GAUSSDB_DATA_MCP_READ_ONLY` | `true` | 拦截 WRITE/DANGEROUS 工具 |
| `GAUSSDB_DATA_MCP_ENABLE_DDL` | `false` | 开启 DANGEROUS（DDL）通道 |
| `GAUSSDB_DATA_MCP_CREDENTIALS_FILE` | 空 | credentials.json 路径 |
| `GAUSSDB_DATA_MCP_TOKENS_FILE` | 空 | HTTP Bearer token 文件（非回环监听必配） |
| `GAUSSDB_DATA_MCP_AUDIT_FILE` | 空 | 审计 JSONL（始终同时写 stderr） |
| `GAUSSDB_DATA_MCP_MAX_ROWS` | `200` | 单次查询行数上限（das 轨同时作为 perpage） |

## 4. 首次使用流程

```text
1) list_user_databases
   ├─ status=ready（dbname 已是用户库）→ 直接执行 SQL
   ├─ status=guidance_needed + user_databases 非空
   │    → 从清单选择，把 dbname 改为目标库（热轮换即生效）
   └─ status=guidance_needed + 无用户库
        → execute_sql_dangerous("CREATE DATABASE mydb")   ← 自动带 panelKey，verified 确认建成
        → dbname 改为 mydb
2) 之后一切操作（读写/建表/DML/Oracle 语法）都在该用户库执行
```

系统库（postgres/template*）只用于查系统目录与建库，**不要**在其中建业务表（pg 库 DDL 会挂死）。

## 5. 工具（11 个）

| 工具 | 通道 | 用途 |
|---|---|---|
| `execute_sql` | READ | 单条只读 SQL（SELECT/SHOW/EXPLAIN/TABLE/WITH…SELECT） |
| `execute_sql_write` | WRITE | 单条 DML（INSERT/UPDATE/DELETE/MERGE） |
| `execute_sql_dangerous` | DANGEROUS | 单条 DDL（确认信封 + `ENABLE_DDL=true`；建库自动带 panelKey 并返回 verified） |
| `explain_sql` | READ | SELECT 类语句的执行计划 |
| `list_schemas` / `list_tables` / `describe_table` | READ | schema/表/列浏览（describe 含列注释） |
| `is_database_connected` / `get_connection_info` | READ | 探活（SELECT 1）/ 池与凭证状态 |
| `list_user_databases` | READ | 用户库清单 + 兼容模式 + 引导（status=ready/guidance_needed） |
| `register_das_connection` | WRITE | 注册 DAS 连接（RegisterDbUser）并写回 credentials——首次接入用 |

每次调用恰好执行一条语句；多语句、UTILITY、危险函数默认拒绝。CREATE/DROP DATABASE 的响应含 `verified` 字段（自动查 pg_database 确认生效——DAS 侧"受理成功"不等于实际执行）。

## 6. 运行期自动机制

| 机制 | 触发 | 行为 |
|---|---|---|
| panelKey | CREATE/DROP DATABASE | 请求自动携带随机 UUID（否则 DAS 包事务执行，GaussDB 拒绝且错误被吞、伪装成功） |
| errorMessage fail-fast | DAS 吞错（状态层全"成功"） | 立即抛 `GAUSSDB_DATA_MCP_SQL_ERROR` |
| verified | 建库/删库完成 | 自动查 pg_database 确认生效 |
| 空壳自愈 | 会话失效（取结果空壳） | 自动 `retry_login`（带密码）并重试一次 |
| 限流退避 | 请求挂死 | 不自动重试，报错交由调用方决策 |

## 7. 使用纪律

- **DAS 对连接有调用频率保护**（超限后无响应式封锁，不返回 429）；DAS 控制台界面与 MCP **共享同一连接配额**——保持低频，请求挂死时**不要重试**（等待或重建连接）
- 业务操作一律在用户库执行；系统库只做建库引导与系统目录查询
- 连接会话失效且 credentials 未配密码时无法自动自愈——要么补配密码，要么控制台重新登录激活

## 8. 部署（streamable-http + Bearer）

```bash
GAUSSDB_DATA_MCP_DRIVER=das \
GAUSSDB_DATA_MCP_CREDENTIALS_FILE=/path/credentials.json \
GAUSSDB_DATA_MCP_TOKENS_FILE=/path/tokens.json \
  uv run gaussdb-data-mcp -t streamable-http
```

客户端：`{"url": "http://<host>:8000/mcp", "headers": {"Authorization": "Bearer <token>"}}`。审计 JSONL 与 token 轮换细节见 `--help` 与源码内注释。

## 许可证

MIT
