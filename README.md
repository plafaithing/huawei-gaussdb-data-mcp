# GaussDB Data MCP Server

> **Language:** [中文](./README_zh.md) | English

## Version

0.1.0

## Overview

Data-plane MCP server for Huawei Cloud GaussDB: AI clients execute SQL against GaussDB
safely over the standard MCP protocol, with three-layer data protection, dual transport
and structured auditing. The server connects to the database instance itself (database
protocol + database account); credentials stay server-side and never reach the LLM. It
complements the management-plane GaussDB MCP (instance lifecycle via OpenAPI/AK-SK):
that one manages the cloud resource, this one works with the data inside it.

## Category

Database

## Features

- 7 tools: SQL execution (read / write / DDL), EXPLAIN, and schema/table/column browsing
- Three risk channels (READ / WRITE / DANGEROUS) with a confirm envelope on the DANGEROUS
  channel; the DDL channel is disabled by default and additionally gated by
  `GAUSSDB_DATA_MCP_ENABLE_DDL=true`
- Server-side SQL allowlist classification plus channel lock: every statement is
  classified by the server (not by the LLM) and must go through the tool matching its
  risk (`GAUSSDB_DATA_MCP_RISK_MISMATCH` otherwise); multi-statement text, utility
  statements, dangerous functions and sensitive session variables are rejected by default
- Read-only by default (`GAUSSDB_DATA_MCP_READ_ONLY=true`); read connections are pinned
  to read-only transactions and reset (`RESET ALL`) before returning to the pool
- Connection pool for reads (fingerprint-validated, idle TTL); write/DDL connections are
  closed after each call
- Dual driver: the official GaussDB psycopg2 driver (Linux/py3.11, required for cloud
  instances) and pyodbc (Windows, via the GaussDB ODBC driver)
- Bearer-token authentication for streamable-http (constant-time comparison, 401/503
  semantics, hot-reloadable token file)
- Structured JSONL audit log per tool call (tool, risk, decision, rules hit, client
  identity, masked params)
- Hot rotation: credentials.json and tokens.json are re-read on every call — atomic file
  replacement, no restart
- Oracle-compatible (A/ORA) databases are a first-class scenario: MERGE INTO upsert,
  DUAL, hierarchical queries, MINUS, sequences and DECODE are verified against real
  centralized and distributed instances

## Available Tools

The data plane exposes no cloud APIs; every tool executes SQL, so each row links the
GaussDB SQL syntax reference covering the statement family of that tool.

| Tool | Scenario | Risk | Documentation |
|---|---|---|---|
| `execute_sql` | One read-only statement: SELECT / VALUES / SHOW / EXPLAIN / TABLE / WITH…SELECT — data queries and exploration | READ | [GaussDB SQL syntax reference](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |
| `execute_sql_write` | One DML statement: INSERT / UPDATE / DELETE / MERGE / REPLACE (autocommit; requires `READ_ONLY=false`) | WRITE | [GaussDB SQL syntax reference](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |
| `execute_sql_dangerous` | One DDL statement: CREATE / ALTER / DROP / TRUNCATE / RENAME / COMMENT / GRANT / REVOKE (confirm envelope + `ENABLE_DDL=true`) | DANGEROUS | [GaussDB SQL syntax reference](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |
| `explain_sql` | EXPLAIN a SELECT-class statement without executing it — check the plan before an expensive query | READ | [GaussDB SQL syntax reference](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |
| `list_schemas` | List database schemas visible to the MCP account | READ | [GaussDB SQL syntax reference](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |
| `list_tables` | List tables and views in a schema (parameterized query) | READ | [GaussDB SQL syntax reference](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |
| `describe_table` | Describe a table's columns: name, type, nullability, default (parameterized query) | READ | [GaussDB SQL syntax reference](https://support.huaweicloud.com/intl/zh-cn/sqlreference-gaussdb/gaussdb_01_0001.html) |

Every tool executes exactly one statement per call.

## Usage Guide

### Prerequisites

- Python 3.10+ and [uv](https://docs.astral.sh/uv/):

**Linux/macOS:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows:**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

- **Database driver.** Cloud GaussDB instances authenticate with sha256, which the
  open-source psycopg2 does not support — use the official driver:

  - **Linux (production):** the official psycopg2 driver ships only inside the GaussDB
    driver package (not on PyPI; Linux x86_64/ARM64 + Python 3.11 only). Extract the
    wheel with `./fetch-driver.sh /path/to/DBS-GaussDB-driver_*.tar.gz`, then
    `pip uninstall psycopg2 psycopg2-binary` and install the extracted wheel. System
    dependencies: `libkeyutils1 libsasl2-2 libldap-common`; when running as a
    non-database user set `LD_LIBRARY_PATH` to
    `<site-packages>/gaussdb_psycopg2.lib`.
  - **Windows (native):** install the GaussDB ODBC driver (`gsqlodbc` installer from the
    official driver package, registered as `GaussDB Unicode`) and `pyodbc`
    (`uv sync --group windows`). Use the official python.org interpreter (3.11+):
    builds whose `_ssl.pyd` links `libcrypto-3-x64.dll` (e.g. uv-managed Python) collide
    with the ODBC driver's DLL of the same name (`IM003 (160)`); the official build names
    it `libcrypto-3.dll` and does not conflict.

- **Database account (third protection layer).** Never use the initial admin account —
  create a dedicated least-privilege role. Read-only baseline (default deployment):

  ```sql
  CREATE ROLE mcp_ro WITH LOGIN PASSWORD '********';
  GRANT USAGE ON SCHEMA mybiz TO mcp_ro;
  GRANT SELECT ON ALL TABLES IN SCHEMA mybiz TO mcp_ro;        -- existing tables
  ALTER DEFAULT PRIVILEGES IN SCHEMA mybiz
    GRANT SELECT ON TABLES TO mcp_ro;                          -- future tables
  ```

  When writes are enabled, use a separate role with only SELECT/INSERT/UPDATE/DELETE on
  the business schema — never CREATE/ALTER/DROP/TRUNCATE/GRANT, so the account layer
  still stops DDL even if the DDL switch is misconfigured. GaussDB locks an account for
  one day after 10 failed password attempts; the server never retries authentication.

### Installation

```bash
git clone https://github.com/huaweicloud/huaweicloud-mcp.git
cd huaweicloud-mcp/server/mcp_server_gaussdb_data
uv sync
uv run mcp_server_gaussdb_data                     # stdio (default)
uv run mcp_server_gaussdb_data -t streamable-http  # remote transport
```

On Linux, put the official driver wheel in place first (see Prerequisites;
`./fetch-driver.sh` extracts it from the official driver package).

### Docker deployment (monorepo build)

The build context is the **repository root** (core is installed directly from the
`libs/huaweicloud_mcp_core` sources, no PyPI release needed). Extract the official
driver wheel into `server/mcp_server_gaussdb_data/wheels/` (not committed) with
`fetch-driver.sh` first:

```bash
cd huaweicloud-mcp
./server/mcp_server_gaussdb_data/fetch-driver.sh /path/to/DBS-GaussDB-driver_*.tar.gz
docker build -t mcp_server_gaussdb_data \
  -f server/mcp_server_gaussdb_data/deploy/Dockerfile .
```

**Multi-arch build**: the Dockerfile picks and validates the driver wheel for the
target arch via `uname -m` inside RUN (under buildx this is the target platform;
wheels for both architectures may coexist in `wheels/`):

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t <registry>/mcp_server_gaussdb_data \
  -f server/mcp_server_gaussdb_data/deploy/Dockerfile --push .
```

> **HCE 2.0 build note (verified in the field)**: on HCE hosts `docker build` RUN
> steps inherit umask 0027 (security baseline); pip artifacts end up 640/750 and the
> in-image non-root user cannot `import`. The Dockerfile sets `umask 022` explicitly
> before pip install; other distros are unaffected.

Runtime settings (`GAUSSDB_DATA_MCP_CREDENTIALS_FILE`/`_TOKENS_FILE`/`_AUDIT_FILE`,
`MCP_SERVER_HOST`, ...) are covered in Configuration; the nginx 443 TLS reverse-proxy
template lives in `deploy/nginx.conf.example`.

## Configuration

### Environment Variables

Credentials can come from a credentials.json file (`GAUSSDB_DATA_MCP_CREDENTIALS_FILE`,
0600, hot-reloadable) or directly from the `GAUSSDB_DATA_MCP_DB_*` environment
variables; the file wins when both are set.

| Variable | Required | Description | Default |
|---|---|---|---|
| `GAUSSDB_DATA_MCP_READ_ONLY` | No | `true` blocks all WRITE/DANGEROUS tools (middleware snapshot — restart to change) | `true` |
| `GAUSSDB_DATA_MCP_ENABLE_DDL` | No | Opens the DANGEROUS (DDL) channel; also needs `READ_ONLY=false`. Hot-read | `false` |
| `GAUSSDB_DATA_MCP_CREDENTIALS_FILE` | No | credentials.json path (0600; hot-reloaded per call) | empty |
| `GAUSSDB_DATA_MCP_TOKENS_FILE` | No | Bearer token file for HTTP; mandatory on non-loopback listen | empty |
| `GAUSSDB_DATA_MCP_GUARDRAILS_CONFIG` | No | Guardrails rules YAML (block/confirm/warn) | empty |
| `GAUSSDB_DATA_MCP_AUDIT_FILE` | No | Audit JSONL file (audit always also goes to stderr) | empty |
| `GAUSSDB_DATA_MCP_PROTECTED_IDS` | No | Protected resource IDs for the guardrails (CSV) | empty |
| `GAUSSDB_DATA_MCP_PROTECTED_NAMES` | No | Protected resource names (CSV) | empty |
| `GAUSSDB_DATA_MCP_PROTECTED_TAGS` | No | Protected resource tags (CSV) | empty |
| `MCP_SERVER_HOST` | No | streamable-http bind address; non-loopback requires `TOKENS_FILE` | `127.0.0.1` |
| `MCP_SERVER_PORT` | No | streamable-http port (1..65535) | `8000` |
| `GAUSSDB_DATA_MCP_MAX_ROWS` | No | Max rows per query; excess rows truncated and flagged | `200` |
| `GAUSSDB_DATA_MCP_MAX_CELL_CHARS` | No | Max characters per cell | `256` |
| `GAUSSDB_DATA_MCP_MAX_SQL_CHARS` | No | Max SQL statement length | `65536` |
| `GAUSSDB_DATA_MCP_STATEMENT_TIMEOUT_MS` | No | Server-side statement_timeout (GaussDB default 0 means no timeout) | `30000` |
| `GAUSSDB_DATA_MCP_POOL_MIN` | No | Read connection pool minimum | `1` |
| `GAUSSDB_DATA_MCP_POOL_MAX` | No | Read connection pool maximum | `8` |
| `GAUSSDB_DATA_MCP_APPLICATION_NAME` | No | Connection application_name (identifies MCP traffic in `pg_stat_activity`) | `gaussdb_data_mcp` |
| `GAUSSDB_DATA_MCP_DRIVER` | No | Force the driver backend: `psycopg2` / `pyodbc` (auto: Windows → pyodbc, others → psycopg2) | auto |
| `GAUSSDB_DATA_MCP_ODBC_DRIVER` | No | ODBC driver registration name for the pyodbc track | `GaussDB Unicode` |
| `GAUSSDB_DATA_MCP_DB_HOST` | No | Env fallback without a credentials file; `host`/`port` have no defaults on purpose — GaussDB CN ports are usually **8000**, the driver default 5432 is wrong | empty |
| `GAUSSDB_DATA_MCP_DB_PORT` | No | DB port | `0` (must be set) |
| `GAUSSDB_DATA_MCP_DB_DBNAME` | No | Database name | empty |
| `GAUSSDB_DATA_MCP_DB_USER` | No | Database user | empty |
| `GAUSSDB_DATA_MCP_DB_PASSWORD` | No | Database password | empty |
| `GAUSSDB_DATA_MCP_DB_SSLMODE` | No | `disable` / `allow` / `prefer` / `require` / `verify-ca` / `verify-full`; public addresses below `require` are rejected (fail-closed) | `prefer` |
| `GAUSSDB_DATA_MCP_DB_SSLROOTCERT` | No | CA certificate path (verify-ca/verify-full; pyodbc track requires it explicitly) | empty |
| `GAUSSDB_DATA_MCP_DB_SSLCERT` | No | Client certificate (gs_hba cert authentication) | empty |
| `GAUSSDB_DATA_MCP_DB_SSLKEY` | No | Client key (gs_hba cert authentication) | empty |

credentials.json fields: `host`, `port`, `dbname`, `user`, `password`, `sslmode`
**Three deployment network topologies for `host`** (choose per enterprise network):

| Topology | host value | Public exposure |
|---|---|---|
| VPC-internal / ECS | DB **private IP** (192.168.x.x) or instance domain | none |
| **Dedicated line / hybrid** | DB **private IP** (intranet reachable via Direct Connect; recommended for enterprises) | none, and no EIP at all |
| Public direct | DB **EIP** (sslmode >= require enforced by guardrail) | public (SSL enforced) |

Dedicated-line topology keeps the intranet IP stable across EIP recycling, eliminating
the EIP-change disconnect issue entirely; both MCP-on-cloud and MCP-on-premises
topologies expose zero public surface for the database.
(plus optional `sslrootcert` / `sslcert` / `sslkey`). See
`credentials.example.json` and `tokens.example.json`.

### Protection blind spots

Guardrails rules match **tool parameters**: resource IDs, name-like parameters and
explicit tag parameters in the tool arguments. The data-plane tools take SQL text
(`query`, `schema`, `table`) as arguments, so the parameter-level resource match
(`resources.ids` / `id_prefixes` / `name_patterns` / `tags`) never fires on SQL text —
it cannot protect a database object by name inside a statement. Protection for this
server works at two levels instead:

1. **Tool level (guardrails, effective):** rules matching on `tools` / `risks` —
   e.g. block or confirm the whole DANGEROUS channel:

   ```yaml
   rules:
     - id: protect-ddl-channel
       action: block          # block / confirm / warn
       message: "DDL is blocked on this deployment; use the approved change window."
       match:
         tools: ["execute_sql_dangerous"]
         risks: ["DANGEROUS"]
   ```

   (See `guardrails.example.yaml`.)

2. **SQL level (three data layers):** statement classification against the SQL allowlist
   with a per-channel tool lock (layer 1), read-only sessions on pooled read connections
   (layer 2), and the least-privilege database account (layer 3). The `GAUSSDB_DATA_MCP_PROTECTED_*`
   variables feed the same guardrails pipeline and therefore act at tool level here too;
   SQL-granular protection is the job of the three layers above.

Rule evaluation runs in the core middleware on top of the read-only gate and the confirm
gate; evaluator errors fail closed (write channels blocked, read channels allowed).

### Run with uvx

From this git repository (subdirectory install):

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

A PyPI-based configuration (`"command": "uvx", "args": ["mcp_server_gaussdb_data"]`)
will be added once the package is published to PyPI.

## Examples

### Claude Code (stdio)

`.mcp.json` (project) or `~/.claude.json`:

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

### Cursor / remote clients (streamable-http + Bearer)

Start the server:

```bash
GAUSSDB_DATA_MCP_CREDENTIALS_FILE=/path/to/credentials.json \
GAUSSDB_DATA_MCP_TOKENS_FILE=/path/to/tokens.json \
MCP_SERVER_HOST=127.0.0.1 \
  uv run mcp_server_gaussdb_data -t streamable-http
```

Client configuration:

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

Then drive it in natural language:

- "List the schemas and tables I can see" → `list_schemas` / `list_tables`
- "Show the columns of the orders table" → `describe_table`
- "Top 10 orders by amount this month" → `execute_sql`
- "Insert this row into orders" → `execute_sql_write` (requires `READ_ONLY=false`)
- "Create an index on orders.created_at" → `execute_sql_dangerous`
  (confirmation envelope, then `confirm=true`)

## License

MIT
