# GaussDB Data MCP Server

> **Language:** English | [中文](./README_zh.md)

GaussDB **data-plane MCP server**: AI clients (Dify, Claude Code, etc.) query and operate GaussDB business data through it under three-layer security protection. Connection goes through the **DAS transport track** — SQL is executed via the Data Admin Service (DAS) control-plane HTTPS interface, so the **database port is never reachable from outside**; the MCP can be deployed anywhere with access to the Huawei Cloud control plane (local or cloud, no VPC / dedicated line / EIP required).

**Oracle-compatible (A/ORA) databases are the first-priority scenario**: MERGE INTO, DUAL, MINUS, DECODE, NVL, varchar2 and other Oracle-style syntax are verified on a real A-compatible database (both DAS track and DB-protocol track).

## 1. Security model

| Layer | Mechanism |
|---|---|
| 1 | Server-side SQL whitelist classification + channel lock (READ/WRITE/DANGEROUS tools; statement/channel mismatch is rejected) |
| 2 | Least-privilege account (business operations via a restricted `mcp_ro`-style account; admin account only for initialization) |
| 3 | Credential domain separation: the database password lives server-side and **never enters the execution path** (das track hands it to DAS once at registration); credentials/SQL never pass through the LLM; one JSONL audit line per call |

On the das track the MCP only holds AK/SK (control-plane signature) + connection id — neither the database password nor connection strings sit on the execution path.

## 2. Install

```bash
uv sync
uv run mcp_server_gaussdb_data            # stdio (default)
uv run mcp_server_gaussdb_data -t streamable-http
```

### 2.1 Configure your AI agent (MCP client)

**stdio** (local agent — Claude Code / Claude Desktop / any standard MCP client). Add to the client's `mcpServers` config:

```json
{
  "mcpServers": {
    "gaussdb-data": {
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/mcp_server_gaussdb_data",
        "mcp_server_gaussdb_data"
      ],
      "env": {
        "GAUSSDB_DATA_MCP_DRIVER": "das",
        "GAUSSDB_DATA_MCP_CREDENTIALS_FILE": "/path/to/credentials.json"
      }
    }
  }
}
```

Claude Code also accepts the shorthand `claude mcp add gaussdb-data -- <command above>`.

**streamable-http** (remote / shared deployment — Dify and HTTP-only clients): start the server as in §8, then point the client at

```json
{"url": "http://<host>:8000/mcp", "headers": {"Authorization": "Bearer <token>"}}
```

Note: Dify supports the HTTP transport only — use the streamable-http form for it. After the first successful connection, verify reachability with `gaussdb_is_database_connected` and pick the database with `gaussdb_list_user_databases` (§4).

## 3. Configuration

### 3.1 credentials.json (0600, hot-reloaded)

```json
{
  "ak": "...", "sk": "...", "region": "cn-north-4",
  "default_instance": "<instance_id>",
  "instances": {
    "<instance_id>": {
      "das_connection_id": "<DAS connection id>",
      "dbname": "mydb",
      "schema": "public",
      "user": "mcp_ro",
      "password": "..."
    }
  }
}
```

| Field | Notes |
|---|---|
| ak/sk | Huawei Cloud AK/SK (control-plane signature; used by DAS interfaces and the automatic IAM project_id lookup) |
| region | Localized names supported ("北京四" → cn-north-4 auto-mapped) |
| das_connection_id | DAS connection id. Source: created when logging into the instance from the DAS console, or produced by the `gaussdb_register_das_connection` tool; **auto-registered when missing** (needs user/password in the entry; RegisterDbUser requires the newer server build) |
| dbname | Database where SQL runs. **If already a user database it is used directly**; when it lands on a system database (postgres etc.) call `gaussdb_list_user_databases` once for selection/creation guidance |
| schema | Default `public` (where business tables/DDL/DML live; use your own business schema if needed). CREATE/DROP DATABASE is routed to the system schema automatically |
| user/password | Auto `retry_login` (with password — the only reliably verified activation form) when the connection is stale; keep it for fully automatic self-healing |

`project_id` needs no configuration (queried from IAM and cached). Multi-instance: several entries under `instances` with `default_instance`; every execution tool accepts an optional `instance` parameter.

### 3.2 Environment variables

| Variable | Default | Notes |
|---|---|---|
| `GAUSSDB_DATA_MCP_DRIVER` | `auto` | set `das` for the DAS track |
| `GAUSSDB_DATA_MCP_READ_ONLY` | `true` | blocks WRITE/DANGEROUS tools |
| `GAUSSDB_DATA_MCP_ENABLE_DDL` | `false` | enables the DANGEROUS (DDL) channel |
| `GAUSSDB_DATA_MCP_CREDENTIALS_FILE` | empty | credentials.json path |
| `GAUSSDB_DATA_MCP_TOKENS_FILE` | empty | Bearer token file (required for non-loopback HTTP) |
| `GAUSSDB_DATA_MCP_AUDIT_FILE` | empty | audit JSONL (always mirrored to stderr) |
| `GAUSSDB_DATA_MCP_MAX_ROWS` | `200` | row cap per query (also the das perpage) |

## 4. First-use flow

```text
1) gaussdb_list_user_databases
   ├─ status=ready (dbname is already a user database) → run SQL directly
   ├─ status=guidance_needed + user_databases non-empty
   │    → pick one, set dbname in the credentials entry (hot-reloaded)
   └─ status=guidance_needed + no user database
        → gaussdb_execute_sql_dangerous("CREATE DATABASE mydb")   ← panelKey auto-applied, verified confirms
        → set dbname to mydb
2) All further operations (read/write/DDL/DML/Oracle syntax) run in that user database
   — tables and business objects live in the database's public (or custom) schema; CREATE/DROP DATABASE is routed automatically, no schema concern
```

System databases (postgres/template*) are only for catalog queries and database creation — **never** create business tables there (pg-database DDL hangs).

## 5. Tools (11)

| Tool | Channel | Purpose |
|---|---|---|
| `gaussdb_execute_sql` | READ | single read-only SQL (SELECT/SHOW/EXPLAIN/TABLE/WITH…SELECT) |
| `gaussdb_execute_sql_write` | WRITE | single DML (INSERT/UPDATE/DELETE/MERGE) |
| `gaussdb_execute_sql_dangerous` | DANGEROUS | single DDL (confirmation envelope + `ENABLE_DDL=true`; CREATE DATABASE auto-applies panelKey and returns verified) |
| `gaussdb_explain_sql` | READ | execution plan for SELECT-class statements |
| `gaussdb_list_schemas` / `gaussdb_list_tables` / `gaussdb_describe_table` | READ | schema/table/column browsing (describe includes column comments) |
| `gaussdb_is_database_connected` / `gaussdb_get_connection_info` | READ | liveness (SELECT 1) / pool & credential status |
| `gaussdb_list_user_databases` | READ | user database list + compatibility mode + guidance (status=ready/guidance_needed) |
| `gaussdb_register_das_connection` | WRITE | register a DAS connection (RegisterDbUser) and persist it to credentials |

Exactly one statement per call; multi-statement, UTILITY and dangerous functions are rejected by default. CREATE/DROP DATABASE responses carry a `verified` field (pg_database is queried automatically — a DAS "accepted" response does not mean it executed).

## 6. Automatic mechanisms

| Mechanism | Trigger | Behavior |
|---|---|---|
| panelKey | CREATE/DROP DATABASE | random UUID auto-applied (otherwise DAS wraps the statement in a transaction, GaussDB rejects it and the error is swallowed as a fake success) |
| errorMessage fail-fast | DAS swallows SQL errors (success-state code/status) | raises `GAUSSDB_DATA_MCP_SQL_ERROR` immediately |
| verified | database created/dropped | pg_database queried automatically to confirm |
| blank-shell self-heal | stale session (empty result shell) | auto `retry_login` (with password) and one retry |
| rate-limit backoff | hung request | no auto-retry; the error is surfaced to the caller |

## 7. Usage discipline

- **DAS enforces call-rate protection per connection** (over-limit manifests as silent hangs, no 429); the DAS console and MCP **share the same connection quota** — keep traffic low and **never retry** a hung request (wait or recreate the connection)
- All business operations run in a user database; system databases are only for catalog queries and database creation
- When the session goes stale and credentials carry no password, self-heal cannot run — either add the password or re-activate the connection from the DAS console

## 8. Deployment (streamable-http + Bearer)

```bash
GAUSSDB_DATA_MCP_DRIVER=das \
GAUSSDB_DATA_MCP_CREDENTIALS_FILE=/path/credentials.json \
GAUSSDB_DATA_MCP_TOKENS_FILE=/path/tokens.json \
  uv run mcp_server_gaussdb_data -t streamable-http
```

Client: `{"url": "http://<host>:8000/mcp", "headers": {"Authorization": "Bearer <token>"}}`.

## License

MIT
