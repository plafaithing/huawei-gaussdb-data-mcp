"""分布式实例（GY1）A 兼容建库探测与清理：通过本服务 MCP execute_sql_dangerous 全链路执行。

用法（live runner 容器内，工作目录 gaussdb-data-mcp）：

    GAUSSDB_DATA_MCP_CREDENTIALS_FILE=/tmp/live-credentials.json \
      python tests/integration/acompat_provision.py create --compat A,PG,C,B \
        --out /tmp/live-credentials-acompat.json
    GAUSSDB_DATA_MCP_CREDENTIALS_FILE=/tmp/live-credentials.json \
      python tests/integration/acompat_provision.py drop

create 流程（每步结果原样打印，供 README 记录事实）：
  0) 读通道查 pg_database 兼容模式清单（实例现有库布局——无论建库成败都记录）
  1) execute_sql_dangerous 无 confirm → confirmation_required 信封（验证确认门）
  2) confirm=true 依次尝试 --compat 列表（默认 A）：
     CREATE DATABASE <db> DBCOMPATIBILITY = '<X>'——分布式可能拒绝特定值
     （GY1 实测：A 被拒，报 InvalidParameterValue: Compatibility args A is invalid），
     第一个成功的值胜出，全部失败则退出 1（runner 据此中断）
  3) 成功则把主凭证的 dbname 换成探测库写出 A 库凭证（0600）供 pytest 使用

环境变量：ACOMPAT_PROBE_DBNAME（默认 _acompat_probe）。
"""

import argparse
import asyncio
import json
import os
import sys

DBNAME = os.environ.get("ACOMPAT_PROBE_DBNAME", "_acompat_probe")
DROP_SQL = f"DROP DATABASE IF EXISTS {DBNAME}"


def _emit(label: str, payload: dict) -> None:
    print(f"[{label}] {json.dumps(payload, ensure_ascii=False)}")


async def _call(server, tool: str, args: dict) -> dict:
    from mcp import Client

    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


def _build_server():
    # READ_ONLY 必须在 build 前置 false（core 中间件构造时快照）；ENABLE_DDL 热读。
    os.environ["GAUSSDB_DATA_MCP_READ_ONLY"] = "false"
    os.environ["GAUSSDB_DATA_MCP_ENABLE_DDL"] = "true"
    from gaussdb_data_mcp.server import build_sql_server

    return build_sql_server()


async def create(out_path: str, compat_list: list[str]) -> int:
    server = _build_server()

    # 0) 实例现有库的兼容模式布局（分布式形态的关键事实）
    listing = await _call(
        server,
        "execute_sql",
        {"query": "SELECT datname, datcompatibility FROM pg_database ORDER BY datname"},
    )
    _emit("step0/pg_database", listing)

    # 1) 无 confirm：确认门应拦截并返回 confirmation_required 信封
    first = await _call(
        server,
        "execute_sql_dangerous",
        {"query": f"CREATE DATABASE {DBNAME} DBCOMPATIBILITY = '{compat_list[0]}'"},
    )
    _emit("step1/no-confirm", first)

    # 2) confirm=true 依次尝试兼容模式值；第一个成功即停
    for compat in compat_list:
        made = await _call(
            server,
            "execute_sql_dangerous",
            {
                "query": f"CREATE DATABASE {DBNAME} DBCOMPATIBILITY = '{compat}'",
                "confirm": True,
            },
        )
        _emit(f"step2/create[{compat}]", made)
        if not made.get("error"):
            main_path = os.environ["GAUSSDB_DATA_MCP_CREDENTIALS_FILE"]
            with open(main_path, encoding="utf-8") as f:
                creds = json.load(f)
            creds["dbname"] = DBNAME
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(creds, f)
            os.chmod(out_path, 0o600)
            print(
                f"RESULT: created with DBCOMPATIBILITY='{compat}'; "
                f"acompat credentials written to {out_path}"
            )
            return 0
    print("RESULT: CREATE DATABASE rejected for all compat values (see step2 envelopes)")
    return 1


async def drop() -> int:
    server = _build_server()
    result = await _call(server, "execute_sql_dangerous", {"query": DROP_SQL, "confirm": True})
    _emit("drop-database", result)
    if result.get("error"):
        print("RESULT: DROP DATABASE failed (see envelope)")
        return 1
    print("RESULT: dropped")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="A-compat probe database provisioning")
    parser.add_argument("action", choices=["create", "drop"])
    parser.add_argument(
        "--compat",
        default="A",
        help="依次尝试的 DBCOMPATIBILITY 值（逗号分隔，默认 A）",
    )
    parser.add_argument("--out", default="/tmp/live-credentials-acompat.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.action == "create":
        rc = asyncio.run(create(args.out, [c.strip() for c in args.compat.split(",")]))
    else:
        rc = asyncio.run(drop())
    sys.exit(rc)


if __name__ == "__main__":
    main()
