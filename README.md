# GaussDB Data MCP Server

GaussDB 507 **数据面 MCP 服务**。AI 客户端（Dify、Claude Code 等）通过它在三层安全防护下查询与操作 GaussDB 里的业务数据；数据库凭证只保存在服务端，不经过 LLM。

**Oracle 兼容（A/ORA）库是第一优先场景**：MERGE INTO、DUAL、层次查询、MINUS、序列、DECODE 等 Oracle 系语法在词法层与真库（集中式 + 分布式）均已验证（实测备忘见[§4.1](#41-aora-兼容库主线场景建库语法与行为两形态实测)）。A 库的 upsert 用 MERGE 表达，而非 PG 的 ON CONFLICT（A 库不支持）：

```sql
MERGE INTO orders t USING staging s ON (t.id = s.id)
WHEN MATCHED THEN UPDATE SET t.qty = s.qty
WHEN NOT MATCHED THEN INSERT VALUES (s.id, s.qty);
```

- 传输：stdio（本地）+ streamable HTTP `/mcp`（远程，Dify 可用）
- 工具：7 个（查询 / 写入 / DDL / EXPLAIN / 元数据浏览），三档风险分级
- 安全底座：`huaweicloud-mcp-core` 1.1.1（Apache 2.0）的 guardrails / 审计 / 脱敏管线
- 运行环境：Linux + Python 3.11（官方驱动硬约束，见[安装](#2-安装)）

## 1. 简介与安全模型

### 1.1 与管控面 MCP 的分工

```
"管房子"（云资源）                              "管数据"（库内 SQL）
gaussdb-mcp（管控面，已有）                      gaussdb-data-mcp（本服务，数据面）
· 华为云 OpenAPI + AK/SK                        · 数据库协议 + 数据库账号
· 实例/备份/参数/扩缩容，265 操作               · execute_sql / 元数据浏览 / 审计
· 无任何 SQL 执行能力                           · 只读默认、通道分级、Bearer 鉴权
        └── 交界：get_instance 提供 private_ips/port ──┘
```

管控面 MCP 管 GaussDB 云资源（创建实例、备份、改参数）；本服务只管库内 SQL。两者使用不同的凭证体系（AK/SK vs 数据库账号），审计日志各自独立。

### 1.2 三层 SQL 防护

任何一层被绕过，下一层仍然兜底：

```
AI 客户端
   │
   │ ① 应用层白名单（sql_guard，服务端强制分类，不依赖 LLM 选对通道）
   │    SELECT / VALUES / SHOW / EXPLAIN / TABLE / WITH…SELECT → READ
   │    INSERT / UPDATE / DELETE / MERGE / REPLACE            → WRITE
   │    CREATE / ALTER / DROP / TRUNCATE / RENAME / COMMENT / GRANT / REVOKE → DANGEROUS
   │    多语句、PREPARE/EXECUTE、SET、COPY、危险函数（pg_read_file/dblink…）、
   │    敏感会话变量（row_security…）、NUL 字节、超长 → 一律拒绝（默认拒绝）
   ▼
   │ ② 会话层（连接池）
   │    只读模式下，读连接建立即 SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY，
   │    归还池前 RESET ALL；写/DDL 走专用连接、autocommit、用完即关
   ▼
   │ ③ 账号层（数据库侧）
   │    默认配只读账号（GRANT SELECT ON ALL TABLES IN SCHEMA + ALTER DEFAULT PRIVILEGES）；
   │    写模式用单独受限账号（见「账号准备」）
   ▼
GaussDB 507
```

第一层之上还有**工具通道锁**：即使分类正确，语句也必须走与其风险匹配的工具（`execute_sql` 收到 UPDATE 会返回 `GAUSSDB_DATA_MCP_RISK_MISMATCH`），DANGEROUS 通道额外需要 `GAUSSDB_DATA_MCP_ENABLE_DDL=true` + 二次确认（confirm 信封）。

> 诚实声明（沿用 AWS mysql-mcp 的话术）：词法白名单不是安全边界，它会保守误拒个别合法语句；真正的边界是只读会话 + 最小权限账号。

### 1.3 同类产品对照

| 维度 | polardb-mcp | AWS mysql-mcp | openGauss 官方 mcp-opengauss | **本服务** |
|---|---|---|---|---|
| 传输 | stdio + SSE（无鉴权） | stdio only | stdio + SSE + streamable HTTP | stdio + streamable HTTP |
| 远程/平台接入 | 裸奔（0.0.0.0 无鉴权） | 无 | 可远程部署 | 内置 Bearer token + 401/503 语义 |
| SQL 只读防护 | 黑名单（实测可被 PREPARE+EXECUTE 绕过） | 黑名单正则 + 危险函数 | 无内置 | **白名单** + 危险函数 + 敏感变量 |
| 会话/账号层 | 无 | 仅文档建议 | 仅文档建议 | 实装（只读会话 + 账号指引） |
| 行数/超时限制 | 无 | 无 | 有执行时间提示 | 行数/单元格/语句长度/超时四项 |
| 审计 | 无 | 无 | 无 | JSONL + 脱敏 + 客户端身份 |
| 凭证 | 环境变量 | Secrets Manager/IAM | 环境变量（不建议明文） | credentials.json 0600 热轮换 |

## 2. 安装

前置要求：Linux + Python 3.11（生产/云端；官方 psycopg2 驱动仅此平台）、Windows + Python 3.11+（原生运行走 pyodbc 轨，见 2.5）或任意 Python 3.11+（无官方驱动的开发环境，见 2.4）。

### 2.1 离线交付包安装（推荐，对齐 gaussdb-mcp 形态）

交付包目录自带 `wheels/huaweicloud_mcp_core-1.1.1-py3-none-any.whl` 与本服务源码：

```bash
chmod +x install.sh run.sh
./install.sh
```

`install.sh` 用 `uv`（有则优先）或 `python3 -m venv` 建立本目录下的 `.venv`，离线装 core wheel + 本服务；若 `wheels/` 下存在 GaussDB 官方驱动 whl（部署者自行从官方发布包放入，不入库），一并安装。校验文件完整性：`sha256sum -c SHA256SUMS`。

### 2.2 uv 在线安装（开发路线）

```bash
cd gaussdb-data-mcp
uv sync          # pyproject 钉死 huaweicloud-mcp-core==1.1.1（本地 wheel）
uv run gaussdb-data-mcp --help
```

### 2.3 官方驱动安装（Linux / Python 3.11，连接云上实例必须）

GaussDB 507 的自研驱动**不发布 PyPI**，只随数据库发布包提供，且**仅 Linux x86_64/ARM64 + Python 3.11**：

1. `wheels/` 已内置**基础驱动 whl**（HCE 2.0 变体，x86_64 + Python 3.11，ECS 实测可用）；
   部署环境为其他 OS（Euler/Suse 等）时，从
   [官方驱动包](https://support.huaweicloud.com/intl/zh-cn/usermanual-gaussdb/gaussdb_01_083.html)
   对应变体目录取 whl **替换 `wheels/` 内同名文件**（各变体 whl 内容不同，文件名相同）；
2. 安装前**清理已装的开源 psycopg2**（官方要求，否则 import 到错误驱动）：
   `pip uninstall psycopg2 psycopg2-binary`；
3. 安装 whl；系统依赖：`libkeyutils1 libsasl2-2 libldap-common`
   （Debian 系 `apt-get install -y libkeyutils1 libsasl2-2 libldap-common`）；
4. 非数据库用户运行时设置库搜索路径：

```bash
export LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/gaussdb_psycopg2.lib
```

不设置时驱动加载报错（对应错误码 `GAUSSDB_DATA_MCP_DRIVER_MISSING` 的另一种成因）。
完整的容器内安装与实测记录见 `tests/integration/README.md`。

**为什么不能用开源 psycopg2**：云上实例认证默认 sha256，开源驱动不支持（实测报
`none of the server's SASL authentication mechanisms are supported`）——这与公网/内网
无关，是认证机制差异；自研驱动对云上实例与本地部署同样适用（本项目两形态均实测验证）。
注意按部署环境取**对应 OS 变体**的驱动包（HCE/Euler/Suse 包内 whl 不同）。

**连接方式**：本服务在数据库视角下就是一个标准客户端（同
[官方手册的连接方式介绍](https://support.huaweicloud.com/intl/zh-cn/usermanual-gaussdb/gaussdb_01_083.html)
中的 gsql/DBeaker/Navicat），网络前提完全一致——同 VPC 内网直连（对应 §6.1 模式 1）
或跨 VPC/公网绑 EIP（对应 §6.2 模式 2）。

### 2.4 Windows 开发环境

官方驱动无 Windows 包。本地开发可用开源 `psycopg2-binary` 连本地 GaussDB 容器
（hba 宽松时可行；**云上实例强制 sha256 认证，开源驱动会被 SASL 拒绝，必须用自研驱动**——
本仓库已实测两轨结论，见 survey/03 补遗）：

```bash
uv sync --group local   # psycopg2-binary 进 [dependency-groups] local，仅本地开发轨；
                         # 驱动仍按 DB-API 2.0 懒加载，代码无需改动
```

### 2.5 Windows 原生运行（pyodbc + GaussDB ODBC）

官方 psycopg2 驱动无 Windows 包（§2.3），本服务内置 **pyodbc 双轨**：装好 GaussDB
ODBC 驱动后即可在 Windows 原生连云上实例（sha256 认证 / SSL / 只读会话 / `?`
占位符均经真实云实例实测通过）。

前置两步：

1. 安装 [GaussDB ODBC 驱动](https://support.huaweicloud.com/intl/zh-cn/usermanual-gaussdb/gaussdb_01_083.html)：
   官方发布包内的 `gsqlodbc` 安装器，安装后自动注册为 ODBC 数据源
   **`GaussDB Unicode`**；
2. `uv pip install pyodbc`（或 `uv sync --group windows`，对应 pyproject 的
   `windows` 依赖组，不进主依赖——Linux 生产轨仍是官方 psycopg2）。

**驱动自动选择**：未配置时 **Windows → pyodbc，其他平台 → psycopg2**；环境变量
`GAUSSDB_DATA_MCP_DRIVER=psycopg2|pyodbc` 可强制指定。ODBC 驱动名默认
`GaussDB Unicode`，非默认安装名可用 `GAUSSDB_DATA_MCP_ODBC_DRIVER` 覆盖。

对调用方透明的差异（服务内部处理）：pyodbc 参数占位符是 `?`（psycopg2 是
`%s`）、`statement_timeout` 通过连接后 `SET` 下发（ODBC 无启动参数）——工具行为
与 Linux 轨完全一致。

**已知问题（已规避）**：Windows 上 GaussDB ODBC DLL 在进程退出的清理阶段触发
堆损坏（exit code `0xC0000374` / `-1073740940`，驱动已知问题，**不影响运行期
功能**）。服务在正常退出路径（stdio EOF / HTTP 关停）已内置规避：跳过解释器
清理直接结束进程（`server.py` 的 `_odbc_exit_guard`）；异常终止路径（如进程被
强杀）仍可能出现一次退出期崩溃报告，属同一已知问题，无数据风险。

## 3. 配置

### 3.0 最小接入（只需要六项）

本地/内网部署，配齐以下六项即可运行，**其余全部可选项**（3.1~3.4 与 guardrails/审计按需）：

```bash
# 方式一：环境变量（零文件）
export GAUSSDB_DATA_MCP_DB_HOST=192.168.0.242
export GAUSSDB_DATA_MCP_DB_PORT=8000
export GAUSSDB_DATA_MCP_DB_USER=mcp_ro
export GAUSSDB_DATA_MCP_DB_PASSWORD='***'
export GAUSSDB_DATA_MCP_DB_DBNAME=postgres
export GAUSSDB_DATA_MCP_DB_SSLMODE=prefer        # 内网默认即可；公网最低 require

# 方式二：一个 credentials.json（六字段，chmod 600）
# {"host": "...", "port": 8000, "dbname": "...", "user": "...", "password": "...", "sslmode": "prefer"}
```

HTTP 对外（ECS 部署）额外需要：`tokens.json`（一行令牌表，见 §3.3）+
`MCP_SERVER_HOST=0.0.0.0`。CA 证书零配置：`sslmode=verify-ca` 时把控制台下载的
`cacert.pem` 放到 credentials.json 旁边即可（见 §3.2）。

### 3.1 环境变量（全量参考）

全部环境变量（名称区分大小写；`MCP_SERVER_*` 沿用 MCP SDK 惯例）：

| 变量 | 默认值 | 安全含义 |
|---|---|---|
| `GAUSSDB_DATA_MCP_READ_ONLY` | `true` | 代码级只读默认：true 时中间件拦截一切 WRITE/DANGEROUS 工具（重启生效） |
| `GAUSSDB_DATA_MCP_ENABLE_DDL` | `false` | DANGEROUS 通道开关（热读，改 env 即生效）；还需 READ_ONLY=false |
| `GAUSSDB_DATA_MCP_CREDENTIALS_FILE` | 无 | 数据库凭证文件路径（每次工具调用热重读） |
| `GAUSSDB_DATA_MCP_TOKENS_FILE` | 无 | HTTP Bearer token 文件；非回环监听时**必配**，否则拒绝启动 |
| `GAUSSDB_DATA_MCP_GUARDRAILS_CONFIG` | 无 | guardrails 规则 YAML（block/confirm/warn） |
| `GAUSSDB_DATA_MCP_AUDIT_FILE` | 无 | 审计 JSONL 追加文件（不配则仅 stderr） |
| `GAUSSDB_DATA_MCP_PROTECTED_IDS` | 空 | 传给 core 的受保护资源 ID（CSV） |
| `GAUSSDB_DATA_MCP_PROTECTED_NAMES` | 空 | 受保护资源名（CSV） |
| `GAUSSDB_DATA_MCP_PROTECTED_TAGS` | 空 | 受保护资源标签（CSV） |
| `MCP_SERVER_HOST` | `127.0.0.1` | HTTP 监听地址；非回环（含 `0.0.0.0`）时强制要求 tokens 文件（fail-closed） |
| `MCP_SERVER_PORT` | `8000` | HTTP 监听端口（1..65535） |
| `GAUSSDB_DATA_MCP_MAX_ROWS` | `200` | 单次查询返回行数上限，超出截断并标记 `truncated` |
| `GAUSSDB_DATA_MCP_MAX_CELL_CHARS` | `256` | 单元格字符截断上限 |
| `GAUSSDB_DATA_MCP_MAX_SQL_CHARS` | `65536` | 单条 SQL 长度上限 |
| `GAUSSDB_DATA_MCP_STATEMENT_TIMEOUT_MS` | `30000` | 服务端 `statement_timeout`（GaussDB 默认 0 不超时，必须设） |
| `GAUSSDB_DATA_MCP_POOL_MIN` / `POOL_MAX` | `1` / `8` | 读连接池尺寸（池内空闲 TTL 600s，低于服务端 session_timeout 1800s） |
| `GAUSSDB_DATA_MCP_APPLICATION_NAME` | `gaussdb_data_mcp` | 连接 `application_name`，便于 `pg_stat_activity` 区分流量 |
| `GAUSSDB_DATA_MCP_DRIVER` | 平台自动 | 强制指定数据库驱动：`psycopg2` / `pyodbc`（未配置时 Windows → pyodbc，其余 → psycopg2，见 §2.5） |
| `GAUSSDB_DATA_MCP_ODBC_DRIVER` | `GaussDB Unicode` | pyodbc 轨的 ODBC 驱动注册名（gsqlodbc 安装器的默认注册名） |
| `GAUSSDB_DATA_MCP_DB_HOST` 等 `DB_*` | 空 | 凭证文件缺省时的环境变量兜底（`DB_PORT` 默认 0 = 必须显式给值） |

`DB_*` 兜底项：`DB_HOST` / `DB_PORT` / `DB_DBNAME` / `DB_USER` / `DB_PASSWORD` /
`DB_SSLMODE`（默认 `prefer`）/ `DB_SSLROOTCERT` / `DB_SSLCERT` / `DB_SSLKEY`。

### 3.2 credentials.json（数据库凭证）

```json
{
  "host": "192.168.0.120",
  "port": 8000,
  "dbname": "postgres",
  "user": "mcp_ro",
  "password": "CHANGE_ME",
  "sslmode": "verify-ca"
}
```

- 权限必须 `0600`（Windows 跳过权限位检查），否则读取即报 `MCP_CREDENTIAL_ERROR`；
- **每次工具调用热重读**：原子替换文件（写临时文件后 `mv`）即可轮换凭证，无需重启；
- `port` 无默认值：GaussDB CN 端口通常 **8000**，驱动默认 5432 是错的（见故障排查）；
- **公网地址护栏（fail-closed）**：host 是公网地址（含域名）而 `sslmode` 低于
  `require`（即 disable/allow/prefer）时拒绝服务，返回
  `GAUSSDB_DATA_MCP_INSECURE_PUBLIC`。VPC 内网地址不受影响。

#### sslmode 取值

| 取值 | 行为 | 适用 |
|---|---|---|
| `disable` | 不加密 | 不应使用 |
| `allow` | 尽量不加密 | 不应使用 |
| `prefer` | 尽量加密（默认值） | VPC 内网 |
| `require` | 加密，不校验服务端身份 | 公网最低要求（护栏强制下限） |
| `verify-ca` | 加密 + 校验 CA 签发 | **生产推荐** |
| `verify-full` | 加密 + CA + 主机名校验 | 最高 |

#### CA 证书（verify-ca / verify-full，三步零配置）

1. 华为云控制台 **实例详情 → SSL 页签** 下载 CA 证书，解压取 `cacert.pem`；
2. 放到 **credentials.json 同目录**，保持文件名 `cacert.pem`；
3. credentials.json 的 `sslmode` 填 `verify-ca` —— 无需其他配置。

找不到证书时服务拒绝启动连接并返回 `GAUSSDB_DATA_MCP_CA_MISSING`（错误信封会给出
期望的证书绝对路径）。高级场景（证书放在别处）可用 `sslrootcert` 字段或
`GAUSSDB_DATA_MCP_DB_SSLROOTCERT` 环境变量显式指定。网络前提（同 VPC / 跨 VPC 绑
EIP / 安全组规则）见官方手册
[GaussDB 实例连接方式介绍](https://support.huaweicloud.com/intl/zh-cn/usermanual-gaussdb/gaussdb_01_083.html)。

> 如数据库侧配置了证书认证（gs_hba method=cert），可通过 credentials.json 的
> `sslcert` / `sslkey` 字段传入客户端证书（非默认场景，密钥文件权限须 600）。

#### 凭证域说明：本服务不需要 AK/SK

本服务是**数据面**：只持有**数据库账号密码**（本文件），通过数据库协议直连实例。
华为云 AK/SK 属于**管控面**（gaussdb-mcp 管实例生命周期）的凭证域，与本服务无关，
也不会经手。

### 3.3 tokens.json（HTTP 客户端鉴权）

```json
{
  "tokens": {
    "dify-prod": "CHANGE_ME_random_long_ascii_token_1",
    "claude-dev": "CHANGE_ME_random_long_ascii_token_2"
  }
}
```

- 每个客户端一个独立 token，token 名进审计日志 `client` 字段；
- token 仅支持 **ASCII 可打印字符**（建议 `[A-Za-z0-9._~-]`，RFC 6750 b64token 字符集），
  违规在加载层直接拒绝；
- 同样 0600 + 热重读，原子替换即轮换（旧 token 立即失效）；
- 鉴权行为：无/错 token → `401` + `WWW-Authenticate: Bearer`；tokens 文件损坏或缺失
  （如原子替换的中间态）→ `503` + `Retry-After: 5`，修复文件后自动恢复；
  scheme 大小写不敏感（`bearer` 与 `Bearer` 等价，RFC 7235）；
  比较用常数时间算法，不泄露 token 内容。

### 3.4 guardrails.yaml（可选规则）

```yaml
rules:
  - id: protect-core-tables
    action: block          # block / confirm / warn
    message: "Core business tables are protected from DDL."
    match:
      tools: ["execute_sql_dangerous"]
      risks: ["DANGEROUS"]
      resources:
        name_patterns: ["^(orders|customers|payments)$"]
```

规则在 core 中间件层评估，与只读门/confirm 门叠加；评估器异常时按 fail-closed 处理
（写通道阻断、读通道放行）。规则命中的请求在审计中记 `rules_hit`。

## 4. 数据库侧准备：A/ORA 兼容主线与账号（第三层防护）

### 4.1 A/ORA 兼容库（主线场景）：建库语法与行为（两形态实测）

Oracle 风格（A 兼容）库是本服务的第一优先场景。建库参数与语法行为**随实例形态而异**
（实测 507.0.0，集中式为本地容器/华为云 BJ4，分布式为华为云 GY1）：

**建库——两形态的 `DBCOMPATIBILITY` 取值互不通用（镜像差异）：**

```sql
-- 集中式：只接受短名 'A'
CREATE DATABASE mydb DBCOMPATIBILITY = 'A';
-- 分布式：只接受全称 'ORA'（'A' 报 InvalidParameterValue: Compatibility args A is invalid）
CREATE DATABASE mydb DBCOMPATIBILITY = 'ORA';
```

- 分布式上 SQL 建库本身可行（实测 root 即可，无需管理面）；管控面 OpenAPI
  `create_database` 请求体**没有** DBCOMPATIBILITY 字段（仅 name/character_set/
  owner/template/…），建 A 库别指望它（除非 template 指向 A 模板库 `templatea`）；
- `pg_database.datcompatibility` 的显示值同样分形态：集中式 `A`/`M`，
  分布式 `ORA`/`MYSQL`/`M`（分布式默认 postgres 库是 MYSQL 兼容，集中式默认 A）；
  按模式过滤库清单时两种写法都要认。

**A 库下的语法行为（两形态行为一致，专项 live 已验证）：**

- **upsert 用 `MERGE INTO`**：PG 式 `ON CONFLICT` 在 A 库被数据库拒绝
  （`syntax error at or near "CONFLICT ("` → `GAUSSDB_DATA_MCP_EXEC_FAILED`）；
- **集合运算符**：`MINUS`（Oracle 形态）与 `EXCEPT`（PG 形态）在 A 库均可用且
  语义等价，`INTERSECT` 正常——三者词法层均归 READ；
- **序列推进由第二层（只读会话）兜底**（实测 507，本地与云端集中式一致）：
  `seq.NEXTVAL`（内核改写为 nextval()）/ `nextval()` / `setval()` 词法层归 READ，
  但只读会话被内核拒绝（`cannot execute nextval()/setval() in a read-only
  transaction` → `GAUSSDB_DATA_MCP_EXEC_FAILED`）——序列无法经 `execute_sql` 推进；
  `CURRVAL` 只读放行，但读池连接上不可能有成功的 NEXTVAL，总是报
  currval undefined；写通道 `INSERT ... VALUES (seq.NEXTVAL)` 是合法 DML，
  不受影响（nextval/setval 刻意不进危险函数名单的原因）；
  `CREATE / DROP / ALTER SEQUENCE` 走 DDL 确认门；
- **空串即 NULL**（Oracle 语义）：`SELECT ''` 返回 NULL、`'' IS NULL` 为真。
  从 PG 迁来的 `WHERE col = ''` 语义会变，请用 `col IS NULL`；
- **层次查询已放行**：`SELECT ... START WITH ... CONNECT BY PRIOR ...` 走 READ 通道
  （词法层曾因 START 误拦，已修复）；DUAL 伪表 / ROWNUM / NVL / `(+)` 外连接正常；
- **Oracle 读函数与优化器提示**：`DECODE` / `TO_CHAR(sysdate, ...)` 真库验证通过；
  优化器提示写在注释里（`SELECT /*+ FULL(t) */ ...`）不触发词法扫描，正常放行；
- PL/SQL 匿名块（`BEGIN ... END;`）仍被多语句门拒绝——刻意取舍，非缺陷。

### 4.2 账号准备（第三层防护）

**绝不使用初始用户（安装时自动生成的管理员，通常与 omm 同名）**：它绕过所有权限检查，
且默认禁止远程连接。一律创建普通业务账号（GaussDB 507 官方 FAQ 原例）。

只读账号（默认部署形态）：

```sql
CREATE ROLE mcp_ro WITH LOGIN PASSWORD '********';
GRANT USAGE ON SCHEMA mybiz TO mcp_ro;                       -- 不授 USAGE 则只能看到对象名
GRANT SELECT ON ALL TABLES IN SCHEMA mybiz TO mcp_ro;        -- 现有表
ALTER DEFAULT PRIVILEGES IN SCHEMA mybiz
  GRANT SELECT ON TABLES TO mcp_ro;                          -- 未来新建表自动授予
```

写账号（放开 READ_ONLY 时，最小权限）：

```sql
CREATE ROLE mcp_rw WITH LOGIN PASSWORD '********';
GRANT USAGE ON SCHEMA mybiz TO mcp_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA mybiz TO mcp_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA mybiz
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mcp_rw;
-- 不授 CREATE/ALTER/DROP/TRUNCATE/GRANT：即使 DDL 开关误开，账号层仍拦得住
```

注意事项：

- **密码连错 10 次锁号 1 天**（`failed_login_attempts` 默认 10、`password_lock_time` 默认 1 天）：
  本服务对认证失败**刻意不重试**，凭证配错不会锁死账号；但请勿用其他工具反复试连；
- 如需审计 MCP 执行的每条 SELECT：`audit_dml_state_select=1` 或把 MCP 账号加入
  `full_audit_users`（注意性能影响，运维决策）；
- 登录/登出/越权/GRANT/REVOKE 默认都在数据库审计日志里（`audit_login_logout=7`）。

## 5. 接入

### 5.1 stdio（Claude Code 等）

`~/.claude.json`（或项目 `.mcp.json`）：

```json
{
  "mcpServers": {
    "gaussdb-data": {
      "command": "/opt/gaussdb-data-mcp/run.sh",
      "env": {
        "GAUSSDB_DATA_MCP_READ_ONLY": "true",
        "GAUSSDB_DATA_MCP_CREDENTIALS_FILE": "/opt/gaussdb-data-mcp/credentials.json",
        "GAUSSDB_DATA_MCP_AUDIT_FILE": "/opt/gaussdb-data-mcp/audit.jsonl",
        "GAUSSDB_DATA_MCP_MAX_ROWS": "200",
        "GAUSSDB_DATA_MCP_STATEMENT_TIMEOUT_MS": "30000"
      }
    }
  }
}
```

stdio 模式走本地信任边界，不经过 Bearer 中间件，审计 `client` 字段为 `stdio`。
Windows 开发机可把 `command` 换成 `uv`、`args` 为 `["run", "gaussdb-data-mcp"]`（工作目录指向本包）。

### 5.2 streamable-http + Dify

服务端启动（`run.sh` 透传参数）：

```bash
MCP_SERVER_HOST=0.0.0.0 \
GAUSSDB_DATA_MCP_TOKENS_FILE=/opt/gaussdb-data-mcp/tokens.json \
  ./run.sh -t streamable-http
```

端点 `http://<host>:8000/mcp`。自检：无 `Authorization` 头 → 401；带
`Authorization: Bearer <token>` 的 initialize 请求 → 200。

Dify（v1.6+）：**Tools → MCP → 添加**，URL 填 `https://<域名>/mcp`，鉴权选
**Custom Header**：`Authorization: Bearer <token>`（Dify 仅支持 HTTP 传输，
不支持 stdio；协议版本 2025-03-26）。

### 5.3 token 轮换

```bash
# 原子替换：写临时文件再 mv，服务无需重启，旧 token 立即失效
printf '%s\n' '{"tokens": {"dify-prod": "<new>"}}' > /tmp/tokens.new
chmod 600 /tmp/tokens.new
mv /tmp/tokens.new /opt/gaussdb-data-mcp/tokens.json
```

数据库凭证（credentials.json）轮换同款操作。

## 6. 生产部署

### 6.1 模式 1（推荐）：VPC 内 MCP + 反代，数据库零公网

```
办公网/平台侧（只持 MCP token，无 DB 凭证）        华为云 VPC
┌────────────────────┐      ┌─────────────────────────────────────────┐
│ Dify / Claude Code │─443─▶│ nginx 反代（TLS 终止 + 限流）            │
└────────────────────┘      │   │ 内网                                 │
                            │   ▼                                     │
                            │ gaussdb-data-mcp（streamable-http:8000，  │
                            │   Bearer 鉴权，凭证不出 VPC）             │
                            │   │ 内网 + SSL + 最小权限账号              │
                            │   ▼                                     │
                            │ GaussDB 507（不绑 EIP，安全组只放 MCP）    │
                            └─────────────────────────────────────────┘
```

要点：GaussDB 不绑 EIP；安全组只放行 MCP 所在主机的源地址；Dify 侧只暴露 443。
nginx 参考配置 `deploy/nginx.conf.example`（`proxy_buffering off` 是 streamable-http
流式必需）。MCP 与 DB 间也建议开 SSL（`sslmode=verify-ca`，`cacert.pem` 放
credentials.json 同目录即可，见 §3.2）。

### 6.2 模式 2（客户场景）：DB 绑 EIP 公网直连

客户把 GaussDB 绑 EIP、MCP 在任意位置直连。本服务的强制护栏：

- **公网 host + `sslmode` 低于 require → 拒绝服务**（`GAUSSDB_DATA_MCP_INSECURE_PUBLIC`，
  fail-closed，无法用配置绕过），凭证必须配 `sslmode=require` 或 `verify-ca`；
- 安全组把 8000 端口限制到 MCP 出口 IP（不要 0.0.0.0/0）；
- 警示公网爆破面：密码错 10 次锁号 1 天会造成 DoS，务必限源。

### 6.3 Docker 部署

```bash
# 构建前：把官方自研驱动 whl 复制进 wheels/（按部署 OS 从官方发布包选对应版本，
# 如 HCE 2.0 x86_64 取 Hce2.0_X86_64 包内 whl；whl 不入库）
cp <官方发布包>/GaussDB_Kernel_507_0_0-2.9.10-py311-none-linux_x86_64.whl wheels/
docker build -t gaussdb-data-mcp -f deploy/Dockerfile .
docker run -d -p 127.0.0.1:8000:8000 \
  -v /opt/mcp/credentials.json:/app/credentials.json:ro \
  -v /opt/mcp/tokens.json:/app/tokens.json:ro \
  -v /opt/mcp/audit:/app/audit \
  -e GAUSSDB_DATA_MCP_CREDENTIALS_FILE=/app/credentials.json \
  -e GAUSSDB_DATA_MCP_TOKENS_FILE=/app/tokens.json \
  -e GAUSSDB_DATA_MCP_AUDIT_FILE=/app/audit/audit.jsonl \
  -e MCP_SERVER_HOST=0.0.0.0 \
  gaussdb-data-mcp
```

镜像内已含 core 与服务，`wheels/` 下的驱动 whl 随构建一并安装，镜像内的
`LD_LIBRARY_PATH` 已指向驱动库目录。非 root 用户 `mcp` 运行；凭证/tokens 文件须
`0600` 且属主与容器内 uid（1000）一致。`-p 127.0.0.1:8000:8000` 只发布到宿主回环，
由 nginx（§6.1）统一暴露 443；`MCP_SERVER_HOST=0.0.0.0` 是容器内监听（端口映射需要），
有 tokens 文件所以通过 fail-closed 检查。

> **HCE 2.0 构建注意（2026-10-12 实测）**：HCE 宿主上 `docker build` 的 RUN 步骤内
> umask 为 0027（安全基线），pip 产物会带 640/750 权限，导致镜像内非 root 用户
> `import` 失败。Dockerfile 已在 pip install 前显式 `umask 022`，其它发行版不受影响。

### 6.4 审计日志

`GAUSSDB_DATA_MCP_AUDIT_FILE` 指定的 JSONL 文件（同时写 stderr），每次工具调用一行：

| 字段 | 说明 |
|---|---|
| `ts` | UTC ISO 时间戳 |
| `event` | 固定 `tool_call` |
| `tool` | 工具名（如 `execute_sql`） |
| `risk` | `READ` / `WRITE` / `DANGEROUS` |
| `decision` | `allowed` / `blocked` / `confirm_required` / `warned` / `fail_closed` |
| `rules_hit` | 命中的 guardrails 规则 ID 列表 |
| `action_taken` | `block` / `confirm` / `warn` / None |
| `params` | 工具入参（`query` 截断到 2000 字符，其余键 200；经脱敏引擎） |
| `duration_ms` | 耗时 |
| `error_code` | **只反映中间件层拦截**（只读门/confirm 门/规则阻断） |
| `client` | `stdio` 或 token 名（HTTP 鉴权身份） |

> **error_code 语义（重要）**：工具**体内**的错误——凭证缺失、连接/认证失败、SQL 执行
> 失败、白名单拒绝、通道不匹配、DDL 开关未开——发生在 SDK 把工具结果转成响应之后，
> 此时 `decision` 仍是 `allowed` 且 `error_code` 为 `null`；排查这类失败要看
> `params.query`（实际执行的 SQL）与工具返回给客户端的错误信封（含 `code`/`hint`）。

> **`client` 字段归属局限（SDK 行为，非本服务缺陷）**：有状态 streamable-http 模式下，
> `client` 取自**会话建立时**的 token 归属——SDK 的 message_router 是会话级长活 task，
> 后续每个请求携带的新 token 更新不会传播到工具执行上下文（contextvar 在会话建立时
> 定型）。因此会话被复用、或多客户端共享同一 MCP session 时，审计中的 `client` 可能
> 显示为会话建立者的 token 名，或回退为 `stdio`。stdio 模式与单客户端独占会话的部署
> 不受影响；需要精确按请求归属审计时，请让每个客户端使用独立会话，并参照 §3.3 为
> 每个客户端配独立 token。

## 7. 工具清单

| 工具 | 风险 | 前置开关 | confirm 语义 | 说明 |
|---|---|---|---|---|
| `execute_sql` | READ | — | 不需要 | 单条只读语句：SELECT / VALUES / SHOW / EXPLAIN / TABLE / WITH…SELECT |
| `execute_sql_write` | WRITE | `READ_ONLY=false`（重启） | 参数保留给 guardrails confirm 规则，默认 false | 单条 DML：INSERT / UPDATE / DELETE / MERGE / REPLACE；autocommit |
| `execute_sql_dangerous` | DANGEROUS | `READ_ONLY=false` + `ENABLE_DDL=true` | 第一次调用返回 `confirmation_required` 信封；复核后带 `confirm=true` 重调 | 单条 DDL：CREATE / ALTER / DROP / TRUNCATE / RENAME / COMMENT / GRANT / REVOKE |
| `explain_sql` | READ | — | 不需要 | 对 SELECT/TABLE/WITH…SELECT 跑 EXPLAIN，不执行 |
| `list_schemas` | READ | — | 不需要 | 列出账号可见 schema |
| `list_tables` | READ | — | 不需要 | 列出 schema 内表与视图（参数化查询） |
| `describe_table` | READ | — | 不需要 | 表列名/类型/可空/默认值（参数化查询） |

所有工具每次恰好执行**一条**语句；语句风险由服务端分类并与通道强制一致
（`GAUSSDB_DATA_MCP_RISK_MISMATCH`）。写通道连接用完即关；读连接复用并在归还前
`RESET ALL`。结果超 `MAX_ROWS` 截断并带 `"truncated": true`。

## 8. 故障排查

| 现象 / 错误码 | 原因与处置 |
|---|---|
| 认证失败（`GAUSSDB_DATA_MCP_AUTH_FAILED`） | 凭证配错。服务**不重试**（防 10 次锁号 1 天），修复 credentials.json 后下次调用自动恢复 |
| 连不上库（`GAUSSDB_DATA_MCP_CONNECT_FAILED`） | **5432 陷阱**：GaussDB CN 端口通常是 **8000**，驱动默认 5432 是错的；`port` 必须显式配置（无默认值是刻意的） |
| 拒连，编码相关报错 | 目标库 `ZHS16GBK` / `GB18030_2022` 编码：官方 Python 驱动 2.9.10 起无法连接，建库须用兼容 PG 的编码（如 UTF8） |
| `GAUSSDB_DATA_MCP_DRIVER_MISSING` | psycopg2 轨：未装官方驱动 whl（或装了开源 psycopg2 未清理），Linux/py3.11 限制与安装步骤见 §2.3；pyodbc 轨：`uv pip install pyodbc` + 安装 GaussDB ODBC 驱动，见 §2.5 |
| `GAUSSDB_DATA_MCP_DRIVER_INVALID` | `GAUSSDB_DATA_MCP_DRIVER` 只接受 `psycopg2` / `pyodbc` |
| Windows 退出时崩溃（exit code `0xC0000374`） | GaussDB ODBC DLL 退出清理堆损坏（已知问题，不影响运行期）；正常退出路径已规避（§2.5），强杀进程仍可见一次崩溃报告，无数据风险 |
| 驱动 import 成功但加载即崩 | `LD_LIBRARY_PATH` 未指向 `.../site-packages/gaussdb_psycopg2.lib`，或缺 `libkeyutils1`/`libsasl2-2`/`libldap-common` |
| `GAUSSDB_DATA_MCP_INSECURE_PUBLIC` | 公网地址 + 弱 sslmode 的 fail-closed 护栏：凭证改 `sslmode=require`/`verify-ca`，或换 VPC 内网地址 |
| `GAUSSDB_DATA_MCP_CA_MISSING` | `verify-ca`/`verify-full` 未找到 CA 证书：把控制台下载的 `cacert.pem` 放到 credentials.json 同目录（hint 给出期望绝对路径），或用 `sslrootcert` 显式指定 |
| `MCP_CREDENTIAL_ERROR` | 凭证/tokens 文件缺失、JSON 非法、权限非 0600、token 含非 ASCII 字符；原子替换为合法文件即可 |
| HTTP 全部 401 | token 不匹配或 scheme 写错（scheme 大小写不敏感，token 本身区分大小写） |
| HTTP 突然全部 503 | tokens 文件处于损坏/缺失状态（如替换到一半）：检查文件，`Retry-After: 5` 后重试 |
| `MCP_READ_ONLY` | 只读门拦截：需要写环境时设 `GAUSSDB_DATA_MCP_READ_ONLY=false` 并重启（hint 会指向该变量） |
| `GAUSSDB_DATA_MCP_DDL_DISABLED` | DDL 通道未开：`GAUSSDB_DATA_MCP_ENABLE_DDL=true`（且 READ_ONLY=false） |
| `GAUSSDB_DATA_MCP_RISK_MISMATCH` | 语句与工具通道不匹配：UPDATE 走 `execute_sql_write`，DDL 走 `execute_sql_dangerous` |
| `GAUSSDB_DATA_MCP_EXEC_FAILED` | 数据库执行报错（语法/权限等）：看信封 message；池内死连接重试一次是安全的 |
| `EXEC_FAILED`：`cannot execute nextval()/setval() in a read-only transaction` | 序列推进被只读会话（第二层）拦截，设计如此：序列无法经 `execute_sql` 推进；写环境用 `execute_sql_write` 的 `INSERT ... VALUES (seq.NEXTVAL)`，或 DDL 通道 `ALTER SEQUENCE`（详见 §4.1 序列条目） |
| `EXEC_FAILED`：`Currval of sequence ... is not yet defined in this session` | CURRVAL 需同会话先成功 NEXTVAL；本服务读通道的 NEXTVAL 一律被只读会话拦截，故经本服务查 CURRVAL 总是此报错（Oracle/PG 本义，非缺陷） |
| 建库报 `Compatibility args ... is invalid` | `DBCOMPATIBILITY` 取值随形态而异：集中式只认 `'A'`，分布式只认 `'ORA'`（详见 §4.1） |
| `GAUSSDB_DATA_MCP_INVALID_IDENTIFIER` | 元数据工具的 schema/table 名不合 `[A-Za-z_][A-Za-z0-9_$]*` |
| `MCP_INVALID_ARGS` | 环境变量值非法（如 READ_ONLY 不是 true/false、PORT 超范围） |

### 实测备忘（Task 9 真库验证沉淀）

1. **EXPLAIN 输出列名**是 `====== Query Others =====`（不是 PG 的 `QUERY PLAN`），
   常量查询的计划行形如 `Parameterize: Yes, parameterized sql: SELECT ?`；
   客户端解析计划时按行文本处理，别按 PG 列名假设。
2. **root 账号的 `$user` schema**：初始账号是 `root`，`search_path="$user",public`
   解析出名为 `root` 的 schema，**不存在名为 `postgres` 的 schema**；用 `list_schemas`
   先看实际布局（系统 schema `dbe_*`/`pkg_*`/`sqladvisor` 等也会列出）。
3. **`python -m` 启动**：`python -m gaussdb_data_mcp.server` 需要 `__main__` guard
   （本版已加入，console script `gaussdb-data-mcp` 与 `python -m` 两种方式均可启动）。

---

测试：`uv run pytest`（单测，含 A 项硬化测试与 Oracle 语法矩阵）；`bash tests/integration/run_live.sh`
（真库 live 套件，需真实实例与官方驱动，Docker 容器内执行）；
`bash tests/integration/run_live_acompat.sh`（A/ORA 兼容专项：建探测 A 库 → DUAL /
空串即 NULL / 层次查询 / MERGE / ON CONFLICT 错误映射 / 序列只读兜底 / MINUS·INTERSECT /
DECODE·TO_CHAR·提示注释 → 清理，默认指向分布式实例，`LIVE_HOST`/`LIVE_PASSWORD_JSON`
可切实例——集中式 BJ4 已抽验 10/10）。
